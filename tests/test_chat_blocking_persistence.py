"""Regression tests: /chat-blocking must persist its transcript to disk
(Discord memory fix, 2026-09-14).

The Discord adapter routes every channel message through /chat-blocking, keyed
``dc:<guild>:<channel>`` so each channel is its own conversation. But
``_chat_blocking_turn`` only called ``_log_session`` on its role-selection
branches -- the terminal branch that appends the real user + assistant
messages set ``_sessions[session_id]`` in memory and returned *without* writing
to disk. The streaming path (``_stream_chat``, which the Telegram adapter's
normal message flow uses) *did* persist.

Net effect: every Discord channel transcript on disk stayed at
``message_count == 1`` (just the ``[ROLE: general]`` line), so any adapter
restart -- and the reload-from-disk in ``_load_or_create_session`` -- handed the
next message an empty history. That is the "missing memory between messages in
the same Discord channel" symptom Gary reported.

These tests exercise ``_chat_blocking_turn`` directly with a fake LLMClient and
real ``_log_session``/``_load_or_create_session`` against a tmp session dir, so
no network/DeepSeek calls are made.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.roles import ROLES
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


class _FakeLLMClient:
    def __init__(self, text: str):
        self._text = text

    def chat(self, system_prompt, history, tools=None):
        return {"choices": [{"message": {"content": self._text, "tool_calls": []}}]}

    def extract_text(self, completion: dict) -> str:
        return completion["choices"][0]["message"].get("content", "") or ""


def _wire(monkeypatch, tmp_path):
    """Real session load/save against tmp_path; stub only the unrelated
    plumbing (role lookup, compaction, turn-report decoration)."""
    monkeypatch.setattr(m, "SESSION_LOG_DIR", tmp_path)
    monkeypatch.setattr(m, "find_role_in_history", lambda history: ROLES["general"])
    monkeypatch.setattr(m, "_gov_name_for_key", lambda pk: None)
    monkeypatch.setattr(m, "_sanitise_tool_messages", lambda history: None)
    monkeypatch.setattr(m, "_maybe_auto_compact", lambda history, sid: False)
    monkeypatch.setattr(m, "_trim_history_to_budget", lambda history: None)
    monkeypatch.setattr(m, "_append_turn_report", lambda text, state: text)
    monkeypatch.setattr(
        m, "_compute_advance_signal", lambda history, trace, session_id=None: None
    )
    m._sessions.clear()


def _session_file(tmp_path, session_id: str):
    h = hashlib.md5(session_id.encode()).hexdigest()[:12]
    return tmp_path / f"{h}.json"


SESSION = "dc:923008087315587072:962081045044424774"


def test_blocking_turn_persists_transcript_to_disk(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        m, "LLMClient", lambda: _FakeLLMClient("Here is my answer about the cacao.")
    )

    resp = asyncio.run(m._chat_blocking_turn(SESSION, "what about the cacao?", "pubkey"))
    assert resp.status_code == 200

    path = _session_file(tmp_path, SESSION)
    assert path.exists(), "blocking turn did not write a session file to disk"

    data = json.loads(path.read_text(encoding="utf-8"))
    contents = [str(x.get("content", "")) for x in data["full_history"]]
    # The user message AND the assistant reply must both be on disk -- not just
    # the role line (the pre-fix message_count == 1 bug).
    assert data["message_count"] > 1, data
    assert any("what about the cacao?" in c for c in contents)
    assert any("Here is my answer about the cacao." in c for c in contents)


def test_transcript_survives_reload_from_disk(monkeypatch, tmp_path):
    """Simulate an adapter restart: drop the in-memory cache and reload the
    session purely from disk. Prior context must come back."""
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "LLMClient", lambda: _FakeLLMClient("First answer."))
    asyncio.run(m._chat_blocking_turn(SESSION, "first question", "pubkey"))

    # Wipe in-memory state, exactly as a process restart would.
    m._sessions.clear()

    reloaded = m._load_or_create_session(SESSION)
    joined = " ".join(str(x.get("content", "")) for x in reloaded)
    assert "first question" in joined
    assert "First answer." in joined
