"""Unit 2 wiring: _maybe_compact_open_turn bounds the *in-flight* turn.

`compact_open_turn` (Unit 1) folds the early rounds of a single still-open turn.
This suite covers the ``main.py`` glue that calls it from the per-round sites:
env knobs, no-op paths, the pre-compaction backup, and -- importantly -- that a
failure inside the folder never blocks the turn.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

import hashlib
from pathlib import Path

import app.main as main_mod
from app.main import _maybe_compact_open_turn

REPO_ROOT = Path(__file__).resolve().parents[1]


def _round(i: int) -> list[dict]:
    """One complete round: assistant tool_calls + its answering tool result."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "ssh_run", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i} " + "x" * 200,
        },
    ]


def _open_turn(n_rounds: int) -> list[dict]:
    """A history whose LAST turn is still open, with n_rounds tool rounds."""
    history = [
        {"role": "user", "content": "older turn"},
        {"role": "assistant", "content": "done"},
    ]
    history.append({"role": "user", "content": "the open turn"})
    for i in range(n_rounds):
        history.extend(_round(i))
    return history


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("CONTEXT_INTRATURN_COMPACTION_AUTO", "0")
    history = _open_turn(20)
    before = list(history)
    assert _maybe_compact_open_turn(history, "sess-kill") is False
    assert history == before


def test_noop_under_round_threshold(monkeypatch):
    monkeypatch.setenv("CONTEXT_INTRATURN_ROUND_THRESHOLD", "8")
    history = _open_turn(3)
    assert _maybe_compact_open_turn(history, "sess-small") is False


def test_folds_over_round_threshold(monkeypatch):
    monkeypatch.setenv("CONTEXT_INTRATURN_ROUND_THRESHOLD", "8")
    monkeypatch.setenv("CONTEXT_INTRATURN_KEEP_LAST_K_ROUNDS", "4")
    monkeypatch.setenv("CONTEXT_INTRATURN_TOKEN_THRESHOLD", "0")  # disable token arm
    monkeypatch.setenv("CONTEXT_INTRATURN_TOKEN_THRESHOLD", "40000")
    history = _open_turn(14)
    n_before = len(history)
    tail_before = history[-8:]  # last 4 rounds = 8 messages

    assert _maybe_compact_open_turn(history, "sess-fold") is True

    assert len(history) < n_before
    # the in-flight tail is preserved byte-identically
    assert history[-8:] == tail_before
    # the synthetic summary is present
    assert any("TURN IN PROGRESS" in str(m.get("content", "")) for m in history)
    # input list object identity is preserved (in-place slice assignment)
    assert history[-1]["role"] == "tool"


def test_writes_pre_compaction_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "SESSION_LOG_DIR", tmp_path)
    monkeypatch.setenv("CONTEXT_INTRATURN_ROUND_THRESHOLD", "8")
    monkeypatch.setenv("CONTEXT_INTRATURN_KEEP_LAST_K_ROUNDS", "4")

    sid = "sess-backup"
    sid_hash = hashlib.md5(sid.encode()).hexdigest()[:12]
    (tmp_path / f"{sid_hash}.json").write_text("[]")

    history = _open_turn(14)
    assert _maybe_compact_open_turn(history, sid) is True
    assert list(tmp_path.glob("*.pre-compact-*.json")), (
        "expected a pre-compaction backup"
    )


def test_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("CONTEXT_INTRATURN_ROUND_THRESHOLD", "1")
    # Malformed entries must degrade to a no-op, never an exception.
    history = [{"role": "user", "content": "hi"}, {"role": "tool", "content": None}, {}]
    assert _maybe_compact_open_turn(history, "sess-garbage") in (True, False)


def test_wired_into_both_per_round_sites():
    """Unit 2's whole point: the folder is actually CALLED by the tool loops."""
    src = (REPO_ROOT / "app" / "main.py").read_text()
    assert "def _maybe_compact_open_turn" in src
    call_sites = src.count("_maybe_compact_open_turn(history, session_id)")
    assert call_sites >= 2, f"expected >=2 per-round call sites, found {call_sites}"
