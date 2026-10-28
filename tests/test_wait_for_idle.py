"""Tests for scripts/wait_for_idle.py -- the deploy restart gate.

Motivation: on 2026-10-28 14:19:32 systemd stopped+started the autopilot while a
turn had a tool call in flight, and that thread went silent. deploy.sh's "drain"
was a bare ``sleep 5``.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wait_for_idle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wait_for_idle_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wfi = _load_module()
NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    import datetime as dt

    return (
        dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _track(
    sid: str,
    *,
    beat_ago: float | None = None,
    started_ago: float = 60.0,
    status: str = "running",
    kind: str = "telegram_chat",
    lease: float = 120.0,
) -> dict:
    track = {
        "id": sid,
        "label": f"chat {sid[-4:]}",
        "track_type": kind,
        "status": status,
        "started_at": _iso(NOW - started_ago),
        "expected_max_duration_s": lease,
        "metadata": {"session_id": sid},
    }
    if beat_ago is not None:
        track["last_heartbeat"] = _iso(NOW - beat_ago)
    return track


# ── the actual regression ──────────────────────────────────────────────────


def test_live_turn_blocks_restart():
    """A turn beating recently must be reported busy."""
    busy = wfi.active_turns({"tracks": [_track("s1", beat_ago=5)]}, now=NOW)
    assert [b["session_id"] for b in busy] == ["s1"]


def test_heartbeat_from_a_long_turn_still_blocks():
    """A turn running 10 minutes but beating now is BUSY, not stale.

    This is the P0 payoff: before the heartbeat ticker existed, a long turn's
    last_heartbeat froze at started_at and looked expired after 120s.
    """
    busy = wfi.active_turns(
        {"tracks": [_track("long", started_ago=600, beat_ago=2)]}, now=NOW
    )
    assert len(busy) == 1


def test_frozen_heartbeat_past_lease_is_not_busy():
    """No heartbeat for longer than the lease => the turn is gone."""
    busy = wfi.active_turns({"tracks": [_track("dead", beat_ago=500)]}, now=NOW)
    assert busy == []


def test_max_age_grace_ignores_ancient_entries():
    """Hard-killed leftovers beyond the grace window must not block forever."""
    track = _track("ghost", beat_ago=300, lease=600.0)
    assert len(wfi.active_turns({"tracks": [track]}, now=NOW, max_age_s=None)) == 1
    assert wfi.active_turns({"tracks": [track]}, now=NOW, max_age_s=180.0) == []


def test_completed_track_is_not_busy():
    busy = wfi.active_turns(
        {"tracks": [_track("done", beat_ago=1, status="completed")]}, now=NOW
    )
    assert busy == []


def test_non_chat_tracks_are_ignored():
    busy = wfi.active_turns(
        {"tracks": [_track("bg", beat_ago=1, kind="inventory_sync")]}, now=NOW
    )
    assert busy == []


def test_missing_timestamps_are_skipped_not_guessed():
    track = _track("nokeys", beat_ago=None)
    track.pop("started_at")
    assert wfi.active_turns({"tracks": [track]}, now=NOW) == []


def test_bare_list_and_empty_shapes():
    assert len(wfi.active_turns([_track("s1", beat_ago=1)], now=NOW)) == 1
    assert wfi.active_turns({"tracks": "not-a-list"}, now=NOW) == []
    assert wfi.active_turns(None, now=NOW) == []


def test_future_timestamp_clamps_to_zero_age():
    busy = wfi.active_turns({"tracks": [_track("skew", beat_ago=-300)]}, now=NOW)
    assert len(busy) == 1 and busy[0]["age_s"] == 0.0


# ── CLI behaviour ─────────────────────────────────────────────────────────


def _write(repo: Path, tracks: list[dict]) -> None:
    (repo / "data").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "active_tracks.json").write_text(
        json.dumps({"version": 1, "tracks": tracks}), encoding="utf-8"
    )


def test_cli_exits_zero_when_idle(tmp_path: Path):
    _write(tmp_path, [])
    assert wfi.main(["--repo", str(tmp_path), "--drain", "0"]) == 0


def test_cli_refuses_when_busy(tmp_path: Path):
    _write(tmp_path, [_track("s1", beat_ago=1)])
    assert wfi.main(["--repo", str(tmp_path), "--drain", "0"]) == 1


def test_cli_force_overrides(tmp_path: Path):
    _write(tmp_path, [_track("s1", beat_ago=1)])
    assert wfi.main(["--repo", str(tmp_path), "--drain", "0", "--force"]) == 0


def test_cli_missing_file_is_abort_by_default(tmp_path: Path):
    """ "Unknown" must not read as "idle": abort so the operator decides."""
    assert wfi.main(["--repo", str(tmp_path), "--drain", "0"]) == 2


def test_cli_missing_file_can_proceed(tmp_path: Path):
    assert (
        wfi.main(["--repo", str(tmp_path), "--drain", "0", "--on-error", "proceed"])
        == 0
    )


def test_cli_corrupt_file_aborts(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "active_tracks.json").write_text("{not json", encoding="utf-8")
    assert wfi.main(["--repo", str(tmp_path), "--drain", "0"]) == 2


def test_cli_waits_for_a_turn_that_finishes(tmp_path: Path, monkeypatch):
    """Drain loop actually re-reads the file and succeeds once the turn ends."""
    _write(tmp_path, [_track("s1", beat_ago=1)])
    calls = {"n": 0}
    real_sleep = time.sleep

    def fake_sleep(_secs):
        calls["n"] += 1
        if calls["n"] >= 2:
            _write(tmp_path, [])  # the turn finished
        real_sleep(0)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    assert wfi.main(["--repo", str(tmp_path), "--drain", "30", "--interval", "1"]) == 0
    assert calls["n"] >= 1


# ── anti-drift: the mirror must agree with the single source of truth ─────


def test_constants_mirror_loop_status():
    from app import loop_status

    assert wfi.DEFAULT_TRACK_LEASE_S == loop_status.DEFAULT_TRACK_LEASE_S
    assert wfi.CHAT_TRACK_TYPES == set(loop_status.CHAT_TRACK_TYPES)


def test_agrees_with_loop_status_on_leases():
    """Same fixture through both parsers => same busy verdict."""
    from app import loop_status

    tracks = [
        _track("live", beat_ago=5),
        _track("expired", beat_ago=500),
        _track("done", beat_ago=1, status="completed"),
        _track("long", started_ago=900, beat_ago=3),
    ]
    mine = {
        b["session_id"]
        for b in wfi.active_turns({"tracks": tracks}, now=NOW, max_age_s=None)
    }
    theirs = {
        lease.session_id
        for lease in loop_status.parse_tracks({"tracks": tracks})
        if lease.is_running and not loop_status.lease_expired(lease, NOW)
    }
    assert mine == theirs == {"live", "long"}


def test_gate_is_executable():
    """deploy.sh invokes it directly, so the +x bit is load-bearing."""
    import os

    assert os.access(_SCRIPT, os.X_OK)
