"""Stale OPEN goals must stop being reported as lost work.

A goal is terminal only on ``complete`` / ``dismiss``. A session that stops
mid-plan writes neither, so its goal stays OPEN indefinitely and ``lost_goal``
re-nudged that thread on every sweep -- long after the underlying work had been
finished and verified. These tests pin the age-out that stops it, and pin the
cases where the old behaviour must be preserved.
"""

import json

from app import loop_daemon, loop_status

NOW = 1_000_000.0
DAY = 86_400.0


def _classify(**kwargs):
    kwargs.setdefault("lease", None)
    kwargs.setdefault("awaiting_age_s", None)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("auto_advance", True)
    return loop_status.classify_session(**kwargs)


def test_fresh_open_goal_still_reports_lost_goal():
    status = _classify(open_goal_text="do the thing", goal_progress_age_s=3_600.0)
    assert status.state == loop_status.STATE_LOST_GOAL
    assert status.state in loop_status.DEFECT_STATES


def test_stale_open_goal_is_idle_not_a_defect():
    # 31 days: the two real goals measured on 2026-10-30.
    status = _classify(open_goal_text="do the thing", goal_progress_age_s=31 * DAY)
    assert status.state == loop_status.STATE_IDLE
    assert status.state not in loop_status.DEFECT_STATES
    assert "stale" in status.reason
    assert status.goal == "do the thing"


def test_staleness_boundary_is_not_crossed_early():
    just_inside = _classify(
        open_goal_text="g", goal_progress_age_s=6.9 * DAY
    )
    assert just_inside.state == loop_status.STATE_LOST_GOAL
    just_outside = _classify(
        open_goal_text="g", goal_progress_age_s=7.1 * DAY
    )
    assert just_outside.state == loop_status.STATE_IDLE


def test_unknown_goal_age_preserves_old_behaviour():
    # An absent timestamp must never silently suppress a real defect.
    status = _classify(open_goal_text="do the thing")
    assert status.state == loop_status.STATE_LOST_GOAL


def test_running_lease_beats_staleness():
    lease = loop_status.Lease(
        session_id="s",
        label="chat",
        status="running",
        last_heartbeat=NOW - 5,
        expected_max_duration_s=120.0,
    )
    status = _classify(
        lease=lease, open_goal_text="g", goal_progress_age_s=99 * DAY
    )
    assert status.state == loop_status.STATE_BUSY


def test_stale_goal_with_no_auto_advance_is_idle():
    status = _classify(
        open_goal_text="g", goal_progress_age_s=99 * DAY, auto_advance=False
    )
    assert status.state == loop_status.STATE_IDLE


def test_no_goal_is_idle_regardless_of_age():
    status = _classify(goal_progress_age_s=999 * DAY)
    assert status.state == loop_status.STATE_IDLE


def _write_goals(tmp_path, *events):
    path = tmp_path / "thread_goals.jsonl"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_load_goal_ages_ages_open_and_skips_terminal(tmp_path, monkeypatch):
    path = _write_goals(
        tmp_path,
        {"event": "open", "session_id": "old", "goal_text": "a", "ts": NOW - 31 * DAY},
        {"event": "open", "session_id": "new", "goal_text": "b", "ts": NOW - 60},
        {
            "event": "open",
            "session_id": "done",
            "goal_text": "c",
            "ts": NOW - 99 * DAY,
        },
        {"event": "complete", "session_id": "done", "ts": NOW - 10},
    )
    monkeypatch.setenv("THREAD_GOALS_PATH", str(path))

    ages = loop_daemon.load_goal_ages(NOW)

    assert set(ages) == {"old", "new"}  # completed goal is not a candidate
    assert ages["old"] > 30 * DAY
    assert ages["new"] < 120


def test_load_goal_ages_prefers_later_progress(tmp_path, monkeypatch):
    # Opened long ago, but a turn reported real progress recently: still live.
    path = _write_goals(
        tmp_path,
        {"event": "open", "session_id": "s", "goal_text": "a", "ts": NOW - 40 * DAY},
        {"event": "turn", "session_id": "s", "ts": NOW - 600, "made_progress": True},
    )
    monkeypatch.setenv("THREAD_GOALS_PATH", str(path))

    ages = loop_daemon.load_goal_ages(NOW)

    assert ages["s"] == 600


def test_load_goal_ages_empty_log(tmp_path, monkeypatch):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("THREAD_GOALS_PATH", str(path))
    assert loop_daemon.load_goal_ages(NOW) == {}
