"""A2 wiring tests: the thread-goal TOOLS + the GOAL_LOOP_ENABLED advance signal.

Track A / unit A2 of SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md. Two invariants matter
most here:

1. **Dark by default.** With GOAL_LOOP_ENABLED off (or AUTO_ADVANCE off), the
   signal must be byte-identical to today -- an open goal yields NO signal.
2. **No cross-thread bleed.** A goal keyed to one session never advances another.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app import thread_goal as tg
    from app.config import settings
    from app.tools import thread_goal_tools as tgt
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"import unavailable: {exc}", allow_module_level=True)


@pytest.fixture
def goal_file(tmp_path, monkeypatch):
    """Point the A1 store at a tmp JSONL so tests never touch real state."""
    p = tmp_path / "thread_goals.jsonl"
    monkeypatch.setattr(tg, "_state_path", lambda: p)
    return p


SID = "MIIBIjANBgkqhkiG9w0B:tg:-1003919341801:36801"


# ── tools ───────────────────────────────────────────────────────────────────


def test_set_tool_records_goal_in_context_session(goal_file):
    out = json.loads(tgt.set_thread_goal("ship the fix", "PR merged", session_id=SID))
    assert out["status"] == "ok"
    assert out["goal"]["status"] == tg.GOAL_OPEN
    assert tg.current_goal(SID).goal_text == "ship the fix"


def test_set_tool_fails_closed_without_session(goal_file):
    out = json.loads(tgt.set_thread_goal("x", session_id=None))
    assert out["status"] == "error"
    assert "session_id" in out["reason"]


def test_set_tool_rejects_empty_goal(goal_file):
    assert json.loads(tgt.set_thread_goal("   ", session_id=SID))["status"] == "error"


def test_complete_tool_marks_done(goal_file):
    tgt.set_thread_goal("g", session_id=SID)
    out = json.loads(tgt.complete_thread_goal(session_id=SID))
    assert out["status"] == "ok"
    assert tg.current_goal(SID).status == tg.GOAL_COMPLETED


def test_complete_tool_no_goal_is_noop(goal_file):
    out = json.loads(tgt.complete_thread_goal(session_id=SID))
    assert out["status"] == "ok"
    assert "nothing to complete" in out["note"]


def test_tools_are_registered():
    from app.tool_registry import get_registry

    r = get_registry()
    assert "set_thread_goal" in r and "complete_thread_goal" in r


# ── signal: both flag states ────────────────────────────────────────────────


def _signal(sid, *, progress=True, gate=True):
    """Call the real signal path with an empty plan scope (plan-less thread)."""
    trace = [{"name": "git_push_changes", "result": "ok"}] if progress else []
    return m._compute_advance_signal(
        [{"role": "user", "content": "do the task"}], trace, sid
    )


def test_flag_off_is_dark_even_with_open_goal(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", False)
    tg.open_goal(SID, "ship it")
    assert _signal(SID) is None  # byte-identical to today


def test_auto_advance_off_is_dark_even_with_goal(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", False)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "ship it")
    assert _signal(SID) is None


def test_flag_on_open_goal_emits_auto(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "ship it")
    sig = _signal(SID, progress=True)
    assert sig is not None
    assert sig["decision"] == "auto" and sig["goal"] is True
    assert sig["next_unit"] == "ship it"
    assert tg.current_goal(SID).turns == 1  # the turn was counted


def test_flag_on_no_progress_stalls(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "ship it")
    assert _signal(SID, progress=False) is None  # stall -> fail closed


def test_flag_on_completed_goal_no_signal(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "ship it")
    tgt.complete_thread_goal(session_id=SID)
    assert _signal(SID, progress=True) is None


def test_flag_on_no_goal_no_signal(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    assert _signal(SID, progress=True) is None


def test_always_stop_in_goal_text_blocks(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "deploy to production")
    assert _signal(SID, progress=True) is None  # always-stop -> no auto


def test_no_cross_thread_bleed(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    other = "MIIBIjANBgkqhkiG9w0B:tg:-1003919341801:99999"
    tg.open_goal(SID, "thread 36801 work")
    assert _signal(other, progress=True) is None  # other thread has no goal
    assert _signal(SID, progress=True) is not None


def test_ceiling_stops_the_loop(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    monkeypatch.setattr(tg, "DEFAULT_MAX_GOAL_TURNS", 3)
    tg.open_goal(SID, "ship it")
    assert _signal(SID) is not None  # turn 1 (auto)
    assert _signal(SID) is not None  # turn 2 (auto)
    assert _signal(SID) is None  # turn 3: turns>=ceiling -> stop
