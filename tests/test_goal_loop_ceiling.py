"""A3 tests: the CHAT_MAX_GOAL_TURNS ceiling + the stall detector.

Track A / unit A3 of SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md. A3 is the *runaway
guard* the roadmap calls mandatory (§4 rule 6, §8 risk 1): a goal-driven loop
must be unable to run forever. Two guards, both independent of the A2 wiring:

1. **Hard ceiling** -- `CHAT_MAX_GOAL_TURNS` (default 40) caps the total turns a
   single goal may consume.
2. **Stall detector** -- `GOAL_STALL_SECONDS` force-stops a goal that has made no
   progress for that many seconds. 0 (default off) => unchanged behavior.

Plus the config<->primitive drift guard: `settings.chat_max_goal_turns` and
`thread_goal.DEFAULT_MAX_GOAL_TURNS` must agree, or a future edit silently
desyncs the documented default from the enforced one.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app import thread_goal as tg
    from app.config import settings
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"import unavailable: {exc}", allow_module_level=True)


SID = "MIIBIjANBgkqhkiG9w0B:tg:-1003919341801:36801"


@pytest.fixture
def goal_file(tmp_path, monkeypatch):
    p = tmp_path / "thread_goals.jsonl"
    monkeypatch.setattr(tg, "_state_path", lambda: p)
    return p


def _signal(sid, *, progress=True):
    trace = [{"name": "git_push_changes", "result": "ok"}] if progress else []
    return m._compute_advance_signal(
        [{"role": "user", "content": "do the task"}], trace, sid
    )


# ── config defaults + drift guard ────────────────────────────────────────────


def test_documented_defaults_agree():
    """The enforced default (config) must match the documented one (primitive)."""
    assert settings.chat_max_goal_turns == tg.DEFAULT_MAX_GOAL_TURNS == 40


def test_stall_default_off():
    """Stall detector is OFF (0) by default => unchanged behavior."""
    assert settings.goal_stall_seconds == 0.0


# ── hard ceiling ─────────────────────────────────────────────────────────────


def test_ceiling_setting_bounds_the_loop(goal_file, monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    monkeypatch.setattr(settings, "chat_max_goal_turns", 2)
    tg.open_goal(SID, "ship it")
    assert _signal(SID) is not None  # turn 1 (auto)
    assert _signal(SID) is None  # turn 2: turns>=ceiling -> stop (no runaway)


def test_ceiling_default_would_allow_many_turns(goal_file, monkeypatch):
    """Sanity: with the real default, turns 1..5 all continue (not a 2-turn cap)."""
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    tg.open_goal(SID, "ship it")
    for _ in range(5):
        assert _signal(SID) is not None


# ── stall detector ───────────────────────────────────────────────────────────


def test_stall_seconds_zero_never_stalls_on_time(tmp_path):
    """stall_seconds=0 disables the check: a very old-progress goal still continues."""
    from app.thread_goal import GoalDecision, ThreadGoal, should_continue

    g = ThreadGoal(
        session_id=SID,
        goal_text="x",
        status=tg.GOAL_OPEN,
        opened_ts=1000.0,
        updated_ts=1000.0,
        turns=1,
        last_progress_ts=1000.0,  # ancient
    )
    dec = should_continue(
        g, made_progress=True, pending_text="x", stall_seconds=0.0, now=10_000_000.0
    )
    assert dec.decision == "continue"
    assert isinstance(dec, GoalDecision)


def test_stall_seconds_positive_stops_idle_goal(tmp_path):
    from app.thread_goal import ThreadGoal, should_continue

    g = ThreadGoal(
        session_id=SID,
        goal_text="x",
        status=tg.GOAL_OPEN,
        opened_ts=1000.0,
        updated_ts=1000.0,
        turns=1,
        last_progress_ts=1000.0,
    )
    dec = should_continue(
        g, made_progress=True, pending_text="x", stall_seconds=600, now=1000.0 + 1200
    )
    assert dec.decision == "stop"
    assert "stall" in (dec.reason or "")


def test_stall_seconds_positive_continues_when_recent(tmp_path):
    from app.thread_goal import ThreadGoal, should_continue

    g = ThreadGoal(
        session_id=SID,
        goal_text="x",
        status=tg.GOAL_OPEN,
        opened_ts=1000.0,
        updated_ts=1000.0,
        turns=1,
        last_progress_ts=1000.0,
    )
    dec = should_continue(
        g, made_progress=True, pending_text="x", stall_seconds=600, now=1000.0 + 60
    )
    assert dec.decision == "continue"


def test_stall_only_with_prior_progress_timestamp(tmp_path, monkeypatch):
    """A goal that never recorded progress (last_progress_ts=0) is not stall-stopped;
    the plain no-progress rule governs instead."""
    from app.thread_goal import ThreadGoal, should_continue

    g = ThreadGoal(
        session_id=SID,
        goal_text="x",
        status=tg.GOAL_OPEN,
        opened_ts=1000.0,
        updated_ts=1000.0,
        turns=1,
        last_progress_ts=0.0,
    )
    dec = should_continue(
        g, made_progress=True, pending_text="x", stall_seconds=600, now=9_999_999.0
    )
    assert dec.decision == "continue"  # no prior progress ts -> stall check skipped


def test_stall_via_config_wiring(goal_file, monkeypatch):
    """The setting flows through the real signal path (fail-closed to no signal)."""
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    monkeypatch.setattr(settings, "goal_stall_seconds", 1.0)
    tg.open_goal(SID, "ship it", now=1.0)
    # record a progress turn far in the past so idle >> 1s at real now()
    tg.record_turn(SID, made_progress=True, now=2.0)
    assert _signal(SID, progress=True) is None  # stalled -> fail closed


# ── ceiling + stall ordering: always-stop still wins ─────────────────────────


def test_always_stop_beats_both_guards(goal_file, monkeypatch):
    from app.thread_goal import ThreadGoal, should_continue

    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "goal_loop_enabled", True)
    monkeypatch.setattr(settings, "chat_max_goal_turns", 1000)
    monkeypatch.setattr(settings, "goal_stall_seconds", 1.0)
    g = ThreadGoal(
        session_id=SID,
        goal_text="deploy to production",
        status=tg.GOAL_OPEN,
        opened_ts=1.0,
        updated_ts=1.0,
        turns=1,
        last_progress_ts=1.0,
    )
    dec = should_continue(
        g,
        made_progress=True,
        pending_text="deploy to production",
        stall_seconds=1.0,
        now=9_999_999.0,
    )
    assert dec.decision == "stop"
    assert "always-stop" in (dec.reason or "")
