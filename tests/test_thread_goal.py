"""Unit tests for app/thread_goal.py -- the Track A (A1) goal store primitive.

See agentic_ai_context/plans/SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md section 4.
Fail-closed is the invariant under test: missing/ambiguous state => stop.
"""

from __future__ import annotations

from app import thread_goal as tg


def _path(tmp_path):
    return tmp_path / "thread_goals.jsonl"


def test_no_goal_is_stop(tmp_path):
    d = tg.should_continue(None, made_progress=True)
    assert d.decision == "stop"
    assert "no open goal" in (d.reason or "")


def test_open_then_current_goal_roundtrip(tmp_path):
    p = _path(tmp_path)
    g = tg.open_goal("tg:1:2", "ship A1", "PR merged", now=100.0, path=p)
    assert g is not None and g.status == tg.GOAL_OPEN
    assert g.goal_text == "ship A1"
    got = tg.current_goal("tg:1:2", path=p)
    assert got is not None and got.goal_text == "ship A1"


def test_open_requires_session_and_text(tmp_path):
    p = _path(tmp_path)
    assert tg.open_goal("", "x", path=p) is None
    assert tg.open_goal("tg:1:2", "", path=p) is None
    assert tg.current_goal("tg:1:2", path=p) is None


def test_progress_turn_continues(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "ship A1", now=100.0, path=p)
    g = tg.record_turn("tg:1:2", made_progress=True, now=101.0, path=p)
    assert g is not None and g.turns == 1
    d = tg.should_continue(g, made_progress=True)
    assert d.decision == "continue"


def test_no_progress_turn_stalls(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "ship A1", now=100.0, path=p)
    g = tg.record_turn("tg:1:2", made_progress=False, now=101.0, path=p)
    d = tg.should_continue(g, made_progress=False)
    assert d.decision == "stop"
    assert "stall" in (d.reason or "")


def test_last_progress_ts_only_advances_on_progress(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "g", now=100.0, path=p)
    tg.record_turn("tg:1:2", made_progress=True, now=101.0, path=p)
    g1 = tg.current_goal("tg:1:2", path=p)
    assert g1 is not None and g1.last_progress_ts == 101.0
    tg.record_turn("tg:1:2", made_progress=False, now=102.0, path=p)
    g2 = tg.current_goal("tg:1:2", path=p)
    assert g2 is not None and g2.last_progress_ts == 101.0  # unchanged


def test_complete_is_done_and_sticky(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "g", now=100.0, path=p)
    g = tg.complete_goal("tg:1:2", now=101.0, path=p)
    assert g is not None and g.status == tg.GOAL_COMPLETED
    d = tg.should_continue(g, made_progress=True)
    assert d.decision == "done"
    # A stray turn after completion must not resurrect the goal.
    tg.record_turn("tg:1:2", made_progress=True, now=102.0, path=p)
    g2 = tg.current_goal("tg:1:2", path=p)
    assert g2 is not None and g2.status == tg.GOAL_COMPLETED


def test_always_stop_halts_even_with_progress(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "g", now=100.0, path=p)
    g = tg.record_turn("tg:1:2", made_progress=True, now=101.0, path=p)
    d = tg.should_continue(g, made_progress=True, pending_text="deploy the box")
    assert d.decision == "stop"
    assert "always-stop" in (d.reason or "")


def test_turn_ceiling_halts(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "g", now=100.0, path=p)
    g = None
    for i in range(3):
        g = tg.record_turn("tg:1:2", made_progress=True, now=101.0 + i, path=p)
    assert g is not None and g.turns == 3
    d = tg.should_continue(g, made_progress=True, max_turns=3)
    assert d.decision == "stop"
    assert "ceiling" in (d.reason or "")


def test_no_cross_thread_bleed(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "goal A", now=100.0, path=p)
    tg.open_goal("tg:9:9", "goal B", now=100.0, path=p)
    a = tg.current_goal("tg:1:2", path=p)
    b = tg.current_goal("tg:9:9", path=p)
    assert a is not None and a.goal_text == "goal A"
    assert b is not None and b.goal_text == "goal B"


def test_reopen_supersedes_previous(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "first", now=100.0, path=p)
    tg.complete_goal("tg:1:2", now=101.0, path=p)
    g = tg.open_goal("tg:1:2", "second", now=102.0, path=p)
    assert g is not None
    assert g.status == tg.GOAL_OPEN and g.goal_text == "second"
    assert g.turns == 0  # fresh goal resets tallies


def test_malformed_line_is_skipped(tmp_path):
    p = _path(tmp_path)
    tg.open_goal("tg:1:2", "g", now=100.0, path=p)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    tg.record_turn("tg:1:2", made_progress=True, now=101.0, path=p)
    g = tg.current_goal("tg:1:2", path=p)
    assert g is not None and g.turns == 1


def test_missing_file_is_none(tmp_path):
    assert tg.current_goal("tg:1:2", path=_path(tmp_path)) is None


def test_should_continue_default_ceiling_constant():
    assert tg.DEFAULT_MAX_GOAL_TURNS == 40
