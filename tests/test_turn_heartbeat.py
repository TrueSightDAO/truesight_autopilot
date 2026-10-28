"""Tests for the chat-turn heartbeat ticker (P0 of the loop-daemon program).

The bug being fixed: ``main.py`` registered a ``telegram_chat`` track but never
called ``deploy_watcher.heartbeat()``, so ``last_heartbeat`` froze at
``started_at``. ``can_deploy`` only blocks while
``now - last_heartbeat < expected_max_duration_s`` (120s for a chat turn), so any
turn longer than 120s silently stopped blocking deploys.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import deploy_watcher, turn_heartbeat


def _iso(seconds_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest.fixture(autouse=True)
def _clean_tickers():
    """No ticker leaks between tests."""
    turn_heartbeat._tasks.clear()
    yield
    turn_heartbeat._tasks.clear()


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point deploy_watcher at a throwaway active_tracks.json."""
    path = tmp_path / "active_tracks.json"
    monkeypatch.setattr(deploy_watcher, "STATE_PATH", path)
    return path


def _write_tracks(path: Path, tracks: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "tracks": tracks}), encoding="utf-8")


def _track(track_id: str = "tg:1:2", seconds_ago: float = 300.0) -> dict:
    return {
        "id": track_id,
        "label": "Telegram chat — governor",
        "track_type": "telegram_chat",
        "started_at": _iso(seconds_ago),
        "last_heartbeat": _iso(seconds_ago),
        "expected_max_duration_s": 120,
        "status": "running",
        "metadata": {"session_id": track_id},
    }


# ── interval_for: the lease can never lapse between beats ────────────────────


def test_interval_is_inside_the_chat_lease():
    assert turn_heartbeat.interval_for("telegram_chat") == 30.0


def test_interval_clamped_to_half_of_a_short_lease():
    # deploy has a 10s-lease neighbour (aws_watcher); half of 10 is 5
    assert turn_heartbeat.interval_for("aws_watcher") == 5.0


def test_interval_never_exceeds_half_the_lease():
    for track_type, lease in deploy_watcher.TRACK_TIMEOUTS.items():
        interval = turn_heartbeat.interval_for(track_type)
        assert interval <= lease / 2.0, f"{track_type} could lapse between beats"
        assert interval >= 1.0


def test_interval_falls_back_for_unknown_track_type():
    assert (
        turn_heartbeat.interval_for("not_a_real_type")
        == turn_heartbeat.DEFAULT_INTERVAL_S
    )


def test_interval_floors_at_one_second():
    """A tiny explicit default must never yield a busy-spin interval."""
    for track_type in deploy_watcher.TRACK_TIMEOUTS:
        assert turn_heartbeat.interval_for(track_type, default=0.001) >= 1.0


# ── env parsing ──────────────────────────────────────────────────────────────


def test_env_interval_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHAT_TURN_HEARTBEAT_INTERVAL_S", "7")
    assert turn_heartbeat._interval_from_env() == 7.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-3"])
def test_bad_env_interval_falls_back(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv("CHAT_TURN_HEARTBEAT_INTERVAL_S", raw)
    assert turn_heartbeat._interval_from_env() == turn_heartbeat.DEFAULT_INTERVAL_S


# ── ticker lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_beats_repeatedly_and_stops_on_cancel():
    seen: list[str] = []

    def beat(track_id: str) -> None:
        seen.append(track_id)

    task = turn_heartbeat.start("tg:1:2", interval=0.01, beat=beat)
    assert task is not None
    await asyncio.sleep(0.08)
    turn_heartbeat.stop("tg:1:2")
    count = len(seen)
    await asyncio.sleep(0.05)

    assert count >= 2, f"expected repeated beats, got {count}"
    assert len(seen) == count, "ticker kept beating after stop()"
    assert all(t == "tg:1:2" for t in seen)


@pytest.mark.asyncio
async def test_start_is_idempotent_per_track():
    first = turn_heartbeat.start("tg:1:2", interval=0.01, beat=lambda _i: None)
    second = turn_heartbeat.start("tg:1:2", interval=0.01, beat=lambda _i: None)
    assert first is second
    turn_heartbeat.stop("tg:1:2")


@pytest.mark.asyncio
async def test_stop_is_safe_when_never_started():
    turn_heartbeat.stop("tg:never")  # must not raise


@pytest.mark.asyncio
async def test_a_raising_beat_does_not_kill_the_ticker():
    calls: list[int] = []

    def flaky_beat(_track_id: str) -> None:
        calls.append(1)
        raise RuntimeError("registry hiccup")

    turn_heartbeat.start("tg:1:2", interval=0.01, beat=flaky_beat)
    await asyncio.sleep(0.08)
    turn_heartbeat.stop("tg:1:2")
    assert len(calls) >= 2, "a single failing beat stopped the ticker"


@pytest.mark.asyncio
async def test_is_running_reflects_lifecycle():
    assert turn_heartbeat.is_running("tg:1:2") is False
    turn_heartbeat.start("tg:1:2", interval=0.01, beat=lambda _i: None)
    assert turn_heartbeat.is_running("tg:1:2") is True
    turn_heartbeat.stop("tg:1:2")
    assert turn_heartbeat.is_running("tg:1:2") is False


@pytest.mark.asyncio
async def test_shutdown_cancels_all_tickers():
    turn_heartbeat.start("a", interval=0.01, beat=lambda _i: None)
    turn_heartbeat.start("b", interval=0.01, beat=lambda _i: None)
    await turn_heartbeat.shutdown()
    assert not turn_heartbeat.is_running("a")
    assert not turn_heartbeat.is_running("b")


# ── the actual regression: a long turn stays deploy-blocking ─────────────────


@pytest.mark.asyncio
async def test_long_turn_keeps_blocking_can_deploy(state_file: Path):
    """The bug: a turn running >120s stopped blocking deploys.

    A track whose last heartbeat is 300s old is *already* past its 120s lease, so
    can_deploy reports 'safe to deploy' -- the mid-turn restart hazard. With the
    ticker beating, the lease stays fresh and the deploy stays blocked.
    """
    _write_tracks(state_file, [_track(seconds_ago=300.0)])

    can, blocking = deploy_watcher.can_deploy()
    assert can is True, "precondition: a frozen lease looks deployable"

    turn_heartbeat.start("tg:1:2", interval=0.01)
    await asyncio.sleep(0.06)
    turn_heartbeat.stop("tg:1:2")

    can, blocking = deploy_watcher.can_deploy()
    assert can is False, "a live turn must block the deploy gate"
    assert [t["id"] for t in blocking] == ["tg:1:2"]


@pytest.mark.asyncio
async def test_frozen_lease_becomes_stalled_then_fresh(state_file: Path):
    """Mirror of the loop-daemon classifier's view of the same field."""
    _write_tracks(state_file, [_track(seconds_ago=500.0)])
    track = deploy_watcher.get_active_tracks()[0]
    assert deploy_watcher._seconds_since(track["last_heartbeat"]) > 120

    await asyncio.to_thread(deploy_watcher.heartbeat, "tg:1:2")

    track = deploy_watcher.get_active_tracks()[0]
    assert deploy_watcher._seconds_since(track["last_heartbeat"]) < 120


def test_heartbeat_ignores_unknown_track(state_file: Path):
    _write_tracks(state_file, [])
    deploy_watcher.heartbeat("tg:absent")  # must not raise, must not add a track
    assert deploy_watcher.get_active_tracks() == []


def test_heartbeat_does_not_revive_a_finished_track(state_file: Path):
    """heartbeat() only touches status == 'running'; a finished track stays out."""
    track = _track(seconds_ago=300.0)
    track["status"] = "completed"
    _write_tracks(state_file, [track])
    deploy_watcher.heartbeat("tg:1:2")
    assert deploy_watcher.get_active_tracks() == []
