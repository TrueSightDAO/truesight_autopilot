"""Parity guard: BOTH chat ingress paths must persist their transcript to disk.

Discord memory fix (thread 29235, truesight_autopilot#442) closed a
one-directional drift between the two chat endpoints:

- ``POST /chat`` (SSE, ``_stream_chat``) -- the Telegram adapter's normal path --
  persisted every turn via ``_log_session``.
- ``POST /chat-blocking`` (``_chat_blocking_turn``) -- the Discord adapter's path --
  did NOT persist its terminal turn, so every Discord channel transcript on disk
  sat at ``message_count == 1`` and an adapter restart handed the next message an
  empty history.

#442 fixed the blocking path and shipped ``test_chat_blocking_persistence.py`` for
it. This module is the *class* guard the fix didn't add: it drives BOTH paths and
asserts each writes the user + assistant turn to disk, so a future refactor that
drops a ``_log_session`` call on *either* caller fails CI instead of silently
breaking memory again. Both endpoints funnel through ``_run_tool_round_loop``;
persisting the final assistant message is each caller's responsibility.
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
    def __init__(self, text: str = "assistant reply"):
        self._text = text

    def chat(self, system_prompt, history, tools=None, **kwargs):
        return {"choices": [{"message": {"content": self._text, "tool_calls": []}}]}

    def extract_text(self, completion: dict) -> str:
        return completion["choices"][0]["message"].get("content", "") or ""


def _wire(monkeypatch, tmp_path, reply="assistant reply"):
    """Real session load/save against tmp_path; stub the unrelated plumbing."""
    monkeypatch.setattr(m, "SESSION_LOG_DIR", tmp_path)
    monkeypatch.setattr(m, "find_role_in_history", lambda history: ROLES["general"])
    monkeypatch.setattr(m, "_gov_name_for_key", lambda pk: None)
    monkeypatch.setattr(m, "_sanitise_tool_messages", lambda history: None)
    monkeypatch.setattr(m, "_maybe_auto_compact", lambda history, sid: False)
    monkeypatch.setattr(m, "_trim_history_to_budget", lambda history: None)
    monkeypatch.setattr(m, "_compact_old_tool_chains", lambda history: None)
    monkeypatch.setattr(m, "_append_turn_report", lambda text, state: text)
    monkeypatch.setattr(m, "_compute_advance_signal", lambda history, trace: None)
    monkeypatch.setattr(m, "get_system_prompt_for_role", lambda role: "sys")
    monkeypatch.setattr(m, "get_tool_schemas_for_role", lambda role: [])
    monkeypatch.setattr(m, "LLMClient", lambda *a, **k: _FakeLLMClient(reply))

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr(m, "_publish_transcript", _no_publish)
    m._sessions.clear()


def _session_file(tmp_path, session_id: str):
    h = hashlib.md5(session_id.encode()).hexdigest()[:12]
    return tmp_path / f"{h}.json"


def _fake_loop_factory(reply: str):
    """Stub the shared tool-round loop: just publish the assistant text, as the
    real loop does via state['assistant_text']. Persisting is the caller's job,
    which is exactly what these tests assert."""

    async def _loop(
        *,
        client,
        system_prompt,
        tools,
        history,
        session_id,
        governor_name,
        req_id,
        state,
        queue_msg_id=None,
    ):
        state["assistant_text"] = reply
        if False:  # pragma: no cover -- keeps this an async generator
            yield ""

    return _loop


async def _drain_streaming(session_id: str, user_message: str):
    # /chat's handler appends the user turn before calling _stream_chat.
    history = m._load_or_create_session(session_id)
    history.append({"role": "user", "content": user_message})
    async for _ in m._stream_chat(
        user_message, history, session_id, role=ROLES["general"]
    ):
        pass


STREAM_SESSION = "tg:-1003919341801:29235"
BLOCKING_SESSION = "dc:923008087315587072:962081045044424774"


def test_streaming_path_persists_turn(monkeypatch, tmp_path):
    """POST /chat (SSE) must persist the user + assistant turn."""
    _wire(monkeypatch, tmp_path, reply="streaming answer")
    monkeypatch.setattr(
        m, "_run_tool_round_loop", _fake_loop_factory("streaming answer")
    )
    asyncio.run(_drain_streaming(STREAM_SESSION, "streaming question"))

    path = _session_file(tmp_path, STREAM_SESSION)
    assert path.exists(), "streaming path did not persist a session file"
    data = json.loads(path.read_text(encoding="utf-8"))
    contents = [str(x.get("content", "")) for x in data["full_history"]]
    assert data["message_count"] > 1, data
    assert any("streaming question" in c for c in contents)
    assert any("streaming answer" in c for c in contents)


def test_blocking_path_persists_turn(monkeypatch, tmp_path):
    """POST /chat-blocking (Discord) must persist the user + assistant turn."""
    _wire(monkeypatch, tmp_path, reply="blocking answer")
    monkeypatch.setattr(m, "LLMClient", lambda: _FakeLLMClient("blocking answer"))
    resp = asyncio.run(
        m._chat_blocking_turn(BLOCKING_SESSION, "blocking question", "pubkey")
    )
    assert resp.status_code == 200

    path = _session_file(tmp_path, BLOCKING_SESSION)
    assert path.exists(), "blocking path did not persist a session file"
    data = json.loads(path.read_text(encoding="utf-8"))
    contents = [str(x.get("content", "")) for x in data["full_history"]]
    assert data["message_count"] > 1, data
    assert any("blocking question" in c for c in contents)
    assert any("blocking answer" in c for c in contents)


def test_both_paths_survive_reload(monkeypatch, tmp_path):
    """A process restart (in-memory cache wiped) must reload BOTH sessions."""
    _wire(monkeypatch, tmp_path, reply="answer")
    monkeypatch.setattr(m, "_run_tool_round_loop", _fake_loop_factory("answer"))
    asyncio.run(_drain_streaming(STREAM_SESSION, "stream q"))
    monkeypatch.setattr(m, "LLMClient", lambda: _FakeLLMClient("answer"))
    asyncio.run(m._chat_blocking_turn(BLOCKING_SESSION, "block q", "pubkey"))

    m._sessions.clear()
    for sid, needle in ((STREAM_SESSION, "stream q"), (BLOCKING_SESSION, "block q")):
        joined = " ".join(
            str(x.get("content", "")) for x in m._load_or_create_session(sid)
        )
        assert needle in joined, f"{sid} lost its context on reload"
