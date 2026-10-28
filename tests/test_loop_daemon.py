"""Tests for the U2 loop daemon (reconciler + publisher)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import loop_daemon, loop_status

NOW = 1_800_000_000.0
LEASE = 120


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_now(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(loop_daemon.time, "time", lambda: NOW)
    return NOW


class FakeRedis:
    """Minimal stand-in capturing hashes and TTLs."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.strings: dict[str, str] = {}
        self.fail_on: set[str] = set()

    def _check(self, key: str) -> None:
        if key in self.fail_on:
            raise RuntimeError("simulated redis failure")

    def delete(self, key: str) -> None:
        self._check(key)
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self._check(key)
        self.hashes.setdefault(key, {}).update(mapping)

    def expire(self, key: str, ttl: int) -> None:
        self._check(key)
        self.ttls[key] = ttl

    def set(self, key: str, value, ex: int | None = None) -> None:
        self._check(key)
        self.strings[key] = str(value)
        if ex is not None:
            self.ttls[key] = ex


def _track(session_id: str, *, started: float, heartbeat: float | None = None) -> dict:
    def iso(ts: float) -> str:
        import datetime as dt

        return (
            dt.datetime.fromtimestamp(ts, dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    return {
        "id": session_id,
        "label": f"chat {session_id[-4:]}",
        "track_type": "telegram_chat",
        "started_at": iso(started),
        "last_heartbeat": iso(heartbeat if heartbeat is not None else started),
        "expected_max_duration_s": LEASE,
        "status": "running",
        "metadata": {"session_id": session_id},
    }


def _write_tracks(path: Path, tracks: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "tracks": tracks}), encoding="utf-8")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated tracks / registries / goal log wired through env vars."""
    tracks = tmp_path / "active_tracks.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    goals = tmp_path / "thread_goals.jsonl"
    goals.write_text("", encoding="utf-8")

    monkeypatch.setenv("ACTIVE_TRACKS_PATH", str(tracks))
    monkeypatch.setenv("SESSION_LOG_DIR", str(sessions))
    monkeypatch.setenv("THREAD_GOALS_PATH", str(goals))
    monkeypatch.setenv("AUTO_ADVANCE", "true")
    return {"tracks": tracks, "sessions": sessions, "goals": goals}


# ── load_tracks ─────────────────────────────────────────────────────────────


def test_load_tracks_missing_file_is_empty(tmp_path: Path) -> None:
    assert loop_daemon.load_tracks(tmp_path / "nope.json") == {}


def test_load_tracks_malformed_json_is_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert loop_daemon.load_tracks(bad) == {}


def test_load_tracks_keys_by_session_id_and_skips_non_dicts(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    _write_tracks(path, [_track("sess-a", started=NOW - 10), None, 3, "x"])  # type: ignore[list-item]
    assert set(loop_daemon.load_tracks(path)) == {"sess-a"}


def test_load_tracks_drops_entries_with_no_usable_id(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps({"tracks": [{"label": "orphan", "started_at": "x"}]}),
        encoding="utf-8",
    )
    assert loop_daemon.load_tracks(path) == {}


def test_load_tracks_keeps_most_recently_active_per_session(tmp_path: Path) -> None:
    """A session can accumulate a stale track; the live one must win."""
    path = tmp_path / "t.json"
    stale = _track("sess-a", started=NOW - 900, heartbeat=NOW - 800)
    live = _track("sess-a", started=NOW - 60, heartbeat=NOW - 5)
    _write_tracks(path, [stale, live])

    tracks = loop_daemon.load_tracks(path)
    assert len(tracks) == 1
    # load_tracks returns Lease objects (parsed floats), not raw track dicts.
    assert tracks["sess-a"].started_at == loop_status.parse_ts(live["started_at"])


def test_load_tracks_falls_back_to_id_when_metadata_absent(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    track = _track("sess-a", started=NOW)
    track.pop("metadata")
    _write_tracks(path, [track])
    assert set(loop_daemon.load_tracks(path)) == {"sess-a"}


# ── load_awaiting ───────────────────────────────────────────────────────────


def test_load_awaiting_reads_both_registries(workspace: dict[str, Path]) -> None:
    (workspace["sessions"] / "_resume_awaiting.json").write_text(
        json.dumps({"m1": {"thread_id": 111, "ts": NOW - 60}}), encoding="utf-8"
    )
    (workspace["sessions"] / "_discord_resume_awaiting.json").write_text(
        json.dumps({"m2": {"channel_id": "222", "ts": NOW - 30}}), encoding="utf-8"
    )

    telegram, discord = loop_daemon.load_awaiting(NOW)
    assert telegram == {"111": NOW - 60}
    assert discord == {"222": NOW - 30}


def test_load_awaiting_missing_registries_are_empty(workspace: dict[str, Path]) -> None:
    assert loop_daemon.load_awaiting(NOW) == ({}, {})


# ── load_goals ──────────────────────────────────────────────────────────────


def _append_goal(path: Path, session_id: str, event: str, **kw) -> None:
    record = {
        "session_id": session_id,
        "event": event,
        "ts": kw.pop("ts", NOW - 100),
        **kw,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def test_load_goals_returns_only_open_goals(workspace: dict[str, Path]) -> None:
    goals = workspace["goals"]
    _append_goal(goals, "open-sess", "open", goal_text="ship U2")
    _append_goal(goals, "done-sess", "open", goal_text="finish U1")
    _append_goal(goals, "done-sess", "complete")

    result = loop_daemon.load_goals()
    assert result == {"open-sess": "ship U2"}


def test_load_goals_empty_when_log_missing(workspace: dict[str, Path]) -> None:
    workspace["goals"].unlink()
    assert loop_daemon.load_goals() == {}


def test_load_goals_skips_unparseable_lines(workspace: dict[str, Path]) -> None:
    goals = workspace["goals"]
    goals.write_text("{bad json\n", encoding="utf-8")
    _append_goal(goals, "s1", "open", goal_text="goal one")
    assert loop_daemon.load_goals() == {"s1": "goal one"}


# ── reconcile ───────────────────────────────────────────────────────────────


def test_reconcile_running_fresh_lease_is_busy(workspace: dict[str, Path]) -> None:
    _write_tracks(
        workspace["tracks"], [_track("s1", started=NOW - 40, heartbeat=NOW - 5)]
    )
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=False)
    assert [s.state for s in sweep.statuses] == ["busy"]
    assert sweep.defects == []


def test_reconcile_expired_lease_is_stalled_defect(workspace: dict[str, Path]) -> None:
    """This is the P0 regression: a frozen heartbeat must read as stalled."""
    _write_tracks(
        workspace["tracks"], [_track("s1", started=NOW - 600, heartbeat=NOW - 400)]
    )
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=False)
    assert [s.state for s in sweep.statuses] == ["stalled"]
    assert len(sweep.defects) == 1


def test_reconcile_open_goal_with_nothing_running_is_lost_goal(
    workspace: dict[str, Path],
) -> None:
    _append_goal(workspace["goals"], "s1", "open", goal_text="finish U2", ts=NOW - 500)
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert [s.state for s in sweep.statuses] == ["lost_goal"]
    assert sweep.statuses[0].goal == "finish U2"


def test_reconcile_open_goal_is_idle_when_auto_advance_off(
    workspace: dict[str, Path],
) -> None:
    _append_goal(workspace["goals"], "s1", "open", goal_text="finish U2", ts=NOW - 500)
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=False)
    assert [s.state for s in sweep.statuses] == ["idle"]
    assert sweep.defects == []


def test_reconcile_fresh_awaiting_beats_lost_goal(workspace: dict[str, Path]) -> None:
    """A governor mid-reply is not a defect, even with a goal open."""
    # Real session keys are "<pubkey>:tg:<chat>:<thread>"; awaiting_age matches on
    # the ":<thread>" suffix, so a bare id can never match.
    key = "PUBKEY:tg:123:456"
    _append_goal(workspace["goals"], key, "open", goal_text="finish U2", ts=NOW - 500)
    (workspace["sessions"] / "_resume_awaiting.json").write_text(
        json.dumps({"m": {"thread_id": "456", "ts": NOW - 60}}), encoding="utf-8"
    )
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert [s.state for s in sweep.statuses] == ["awaiting"]


def test_reconcile_nothing_at_all_yields_no_statuses(
    workspace: dict[str, Path],
) -> None:
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert sweep.statuses == []


def test_reconcile_unions_track_and_goal_sessions(workspace: dict[str, Path]) -> None:
    _write_tracks(workspace["tracks"], [_track("tracked", started=NOW - 5)])
    _append_goal(workspace["goals"], "goalonly", "open", goal_text="never started")
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert {s.session_id for s in sweep.statuses} == {"tracked", "goalonly"}


def test_reconcile_dedupes_session_present_in_both_sources(
    workspace: dict[str, Path],
) -> None:
    _write_tracks(workspace["tracks"], [_track("s1", started=NOW - 5)])
    _append_goal(workspace["goals"], "s1", "open", goal_text="g")
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert len(sweep.statuses) == 1


def test_reconcile_bounds_the_published_set(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop_daemon, "MAX_SESSIONS", 3)
    _write_tracks(
        workspace["tracks"], [_track(f"s{i}", started=NOW - 5) for i in range(10)]
    )
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=True)
    assert len(sweep.statuses) == 3


def test_reconcile_label_is_propagated(workspace: dict[str, Path]) -> None:
    _write_tracks(workspace["tracks"], [_track("s1", started=NOW - 5)])
    sweep = loop_daemon.reconcile(now=NOW, auto_advance=False)
    assert sweep.statuses[0].label == "chat s1"


def test_reconcile_auto_advance_reads_env(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _append_goal(workspace["goals"], "s1", "open", goal_text="g", ts=NOW - 500)
    for raw, expected in [("true", "lost_goal"), ("false", "idle"), ("0", "idle")]:
        monkeypatch.setenv("AUTO_ADVANCE", raw)
        sweep = loop_daemon.reconcile(now=NOW)
        assert sweep.statuses[0].state == expected, raw


# ── publish ─────────────────────────────────────────────────────────────────


def _sweep_with(*states: tuple[str, str]) -> loop_daemon.Sweep:
    sweep = loop_daemon.Sweep()
    for session_id, state in states:
        sweep.statuses.append(
            loop_status.SessionStatus(session_id=session_id, state=state, reason="r")
        )
    return sweep


def test_publish_writes_one_hash_per_session() -> None:
    fake = FakeRedis()
    written = loop_daemon.publish(
        _sweep_with(("a", "busy"), ("b", "stalled")), client=fake, interval=60, now=NOW
    )
    assert written == 2
    assert set(fake.hashes) == {"loop:status:a", "loop:status:b"}


def test_publish_ttl_is_three_intervals() -> None:
    """A dead daemon's verdicts must evaporate, not linger."""
    fake = FakeRedis()
    loop_daemon.publish(_sweep_with(("a", "busy")), client=fake, interval=60, now=NOW)
    assert fake.ttls["loop:status:a"] == 60 * loop_daemon.TTL_MULTIPLIER == 180


def test_publish_heartbeat_key_is_ttld() -> None:
    fake = FakeRedis()
    loop_daemon.publish(loop_daemon.Sweep(), client=fake, interval=60, now=NOW)
    assert fake.strings[loop_daemon.DAEMON_KEY] == str(int(NOW))
    assert fake.ttls[loop_daemon.DAEMON_KEY] == 180


def test_publish_includes_state_in_payload() -> None:
    fake = FakeRedis()
    loop_daemon.publish(
        _sweep_with(("a", "stalled")), client=fake, interval=60, now=NOW
    )
    assert fake.hashes["loop:status:a"]["state"] == "stalled"


def test_publish_survives_a_single_key_failure() -> None:
    fake = FakeRedis()
    fake.fail_on.add("loop:status:boom")
    written = loop_daemon.publish(
        _sweep_with(("boom", "stalled"), ("ok", "busy")),
        client=fake,
        interval=60,
        now=NOW,
    )
    assert written == 1
    assert set(fake.hashes) == {"loop:status:ok"}


def test_publish_still_beats_when_all_statuses_fail() -> None:
    fake = FakeRedis()
    fake.fail_on.add("loop:status:boom")
    loop_daemon.publish(
        _sweep_with(("boom", "stalled")), client=fake, interval=60, now=NOW
    )
    assert loop_daemon.DAEMON_KEY in fake.strings


def test_publish_zero_interval_clamps_ttl_to_positive() -> None:
    fake = FakeRedis()
    loop_daemon.publish(_sweep_with(("a", "busy")), client=fake, interval=0, now=NOW)
    assert fake.ttls["loop:status:a"] >= 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_main_once_dry_run_publishes_nothing(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_tracks(workspace["tracks"], [_track("s1", started=NOW - 5)])
    assert loop_daemon.main(["--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "loop_daemon:" in out
    assert "published" not in out


def test_main_once_reports_publish_count(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    _write_tracks(workspace["tracks"], [_track("s1", started=NOW - 5)])
    fake = FakeRedis()
    monkeypatch.setattr(loop_daemon, "_client", lambda: fake)
    assert loop_daemon.main(["--once"]) == 0
    assert "published 1 status key" in capsys.readouterr().out


def test_format_sweep_summarises_states() -> None:
    text = loop_daemon._format_sweep(_sweep_with(("a", "busy"), ("b", "stalled")))
    assert "busy=1" in text and "stalled=1" in text


def test_format_sweep_lists_defects_only() -> None:
    text = loop_daemon._format_sweep(_sweep_with(("a", "busy"), ("b", "lost_goal")))
    assert "lost_goal" in text
    assert text.count("\n") == 1


def test_format_sweep_handles_no_sessions() -> None:
    assert "no sessions" in loop_daemon._format_sweep(loop_daemon.Sweep())
