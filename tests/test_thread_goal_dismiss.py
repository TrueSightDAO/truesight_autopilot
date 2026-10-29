"""Dismissing a thread goal: terminal, but never mistaken for completed.

A governor may decide an open goal is no longer wanted. Before this existed the
only exit was ``complete_goal``, so dropping a goal meant either lying about it
being delivered or being nagged by the loop-alert sweep forever.
"""

from __future__ import annotations

import json

import pytest

from app import loop_daemon, loop_status, thread_goal
from app.tools import thread_goal_tools


@pytest.fixture()
def goal_log(tmp_path, monkeypatch):
    path = tmp_path / "thread_goals.jsonl"
    path.write_text("")
    monkeypatch.setenv("THREAD_GOALS_PATH", str(path))
    monkeypatch.setattr(thread_goal, "_state_path", lambda: path)
    return path


# ── state machine ────────────────────────────────────────────────────────────


def test_dismiss_is_terminal(goal_log):
    thread_goal.open_goal("s1", "ship the thing", "it works", path=goal_log)
    g = thread_goal.dismiss_goal("s1", reason="superseded", path=goal_log)
    assert g is not None
    assert g.status == thread_goal.GOAL_DISMISSED
    assert g.goal_text == "ship the thing"


def test_dismiss_is_distinct_from_complete(goal_log):
    thread_goal.open_goal("a", "delivered work", path=goal_log)
    thread_goal.complete_goal("a", path=goal_log)
    thread_goal.open_goal("b", "dropped work", path=goal_log)
    thread_goal.dismiss_goal("b", reason="not wanted", path=goal_log)

    assert (
        thread_goal.current_goal("a", path=goal_log).status
        == thread_goal.GOAL_COMPLETED
    )
    assert (
        thread_goal.current_goal("b", path=goal_log).status
        == thread_goal.GOAL_DISMISSED
    )
    # ...and the distinction survives in the raw log, not just in memory.
    events = [
        json.loads(line) for line in goal_log.read_text().splitlines() if line.strip()
    ]
    kinds = [e["event"] for e in events]
    assert "complete" in kinds and "dismiss" in kinds


def test_dismiss_records_reason(goal_log):
    thread_goal.open_goal("s1", "g", path=goal_log)
    thread_goal.dismiss_goal("s1", reason="obsolete after the migration", path=goal_log)
    events = [
        json.loads(line) for line in goal_log.read_text().splitlines() if line.strip()
    ]
    dismiss = [e for e in events if e["event"] == "dismiss"][0]
    assert dismiss["reason"] == "obsolete after the migration"


def test_dismiss_is_sticky_against_later_turns(goal_log):
    thread_goal.open_goal("s1", "g", "crit", path=goal_log)
    thread_goal.dismiss_goal("s1", path=goal_log)
    g = thread_goal.record_turn("s1", made_progress=False, path=goal_log)
    assert g is not None
    assert g.status == thread_goal.GOAL_DISMISSED, (
        "a stray turn must not resurrect a dismissed goal"
    )


def test_reopen_supersedes_dismiss(goal_log):
    thread_goal.open_goal("s1", "old", path=goal_log)
    thread_goal.dismiss_goal("s1", path=goal_log)
    g = thread_goal.open_goal("s1", "new goal", path=goal_log)
    assert g.status == thread_goal.GOAL_OPEN
    assert g.goal_text == "new goal"


def test_dismiss_without_open_goal_is_harmless(goal_log):
    g = thread_goal.dismiss_goal("never-seen", path=goal_log)
    assert g is None


def test_dismiss_empty_session_id_is_harmless(goal_log):
    assert thread_goal.dismiss_goal("", path=goal_log) is None


def test_dismiss_does_not_stop_auto_advance_before_open(goal_log):
    """Dismissing with no goal must not fabricate a terminal state."""
    thread_goal.dismiss_goal("s1", path=goal_log)
    assert thread_goal.current_goal("s1", path=goal_log) is None


# ── the integration that actually silences the alert ─────────────────────────


def test_load_goals_excludes_dismissed(goal_log):
    """THE point of this change: a dismissed goal stops surfacing as lost_goal."""
    thread_goal.open_goal("open_sess", "still real work", path=goal_log)
    thread_goal.open_goal("dropped_sess", "abandoned work", path=goal_log)
    thread_goal.dismiss_goal("dropped_sess", reason="dropped", path=goal_log)
    thread_goal.open_goal("done_sess", "finished work", path=goal_log)
    thread_goal.complete_goal("done_sess", path=goal_log)

    goals = loop_daemon.load_goals()
    assert set(goals) == {"open_sess"}
    assert goals["open_sess"] == "still real work"


def test_dismissed_goal_classifies_as_not_lost(goal_log):
    """End-to-end: the classifier sees no goal, so it cannot report lost_goal."""
    thread_goal.open_goal("s1", "abandoned", path=goal_log)
    thread_goal.dismiss_goal("s1", path=goal_log)
    goals = loop_daemon.load_goals()

    st = loop_status.classify_session(
        "s1",
        lease=None,
        awaiting_age_s=None,
        open_goal_text=goals.get("s1"),
        now=thread_goal.time.time(),
        auto_advance=True,
    )
    # With auto_advance ON an undismissed goal WOULD classify lost_goal (see the
    # control below), so this is a real assertion, not a vacuous idle check.
    assert st.state != loop_status.STATE_LOST_GOAL
    assert st.state == loop_status.STATE_IDLE


def test_open_goal_still_reports_lost(goal_log):
    """Control: an undismissed goal must still raise lost_goal (no over-suppression)."""
    thread_goal.open_goal("s1", "real outstanding work", path=goal_log)
    goals = loop_daemon.load_goals()
    st = loop_status.classify_session(
        "s1",
        lease=None,
        awaiting_age_s=None,
        open_goal_text=goals.get("s1"),
        now=thread_goal.time.time(),
        auto_advance=True,
    )
    assert st.state == loop_status.STATE_LOST_GOAL


# ── the agent-facing tool ────────────────────────────────────────────────────


def test_tool_dismisses_and_reports_status(goal_log):
    thread_goal.open_goal("s9", "do a thing", path=goal_log)
    out = json.loads(
        thread_goal_tools.dismiss_thread_goal("changed plans", session_id="s9")
    )
    assert out["status"] == "ok"
    assert out["goal"]["status"] == thread_goal.GOAL_DISMISSED
    assert out["reason"] == "changed plans"
    assert "not completed" in out["note"].lower() or "dismissed" in out["note"].lower()


def test_tool_on_terminal_goal_is_noop(goal_log):
    thread_goal.open_goal("s9", "g", path=goal_log)
    thread_goal.complete_goal("s9", path=goal_log)
    out = json.loads(thread_goal_tools.dismiss_thread_goal("", session_id="s9"))
    assert out["status"] == "ok"
    assert out["goal"]["status"] == thread_goal.GOAL_COMPLETED
    assert (
        thread_goal.current_goal("s9", path=goal_log).status
        == thread_goal.GOAL_COMPLETED
    )


def test_tool_without_session_id_errors():
    out = json.loads(thread_goal_tools.dismiss_thread_goal("", session_id=None))
    assert out["status"] == "error"


def test_tool_without_goal_reports_nothing_to_dismiss(goal_log):
    out = json.loads(thread_goal_tools.dismiss_thread_goal("", session_id="ghost"))
    assert out["status"] == "ok"
    assert "nothing to dismiss" in out["note"]


def test_tool_is_registered():
    names = {spec.name for spec in thread_goal_tools.TOOL_SPECS}
    assert "dismiss_thread_goal" in names
    assert "complete_thread_goal" in names
