"""Tests for app/deploy_watcher.py — safe deploy orchestration."""

import json
import tempfile
from pathlib import Path

import pytest

from app.deploy_watcher import (
    STATE_PATH,
    can_deploy,
    get_active_tracks,
    get_system_status,
    heartbeat,
    register_track,
    unregister_track,
)


@pytest.fixture(autouse=True)
def _temp_state():
    """Use a temp directory for state files so tests don't collide."""
    with tempfile.TemporaryDirectory(prefix="deploy_watcher_test_") as tmpdir:
        original = STATE_PATH
        # Override the module-level constant
        import app.deploy_watcher as dw

        dw.STATE_PATH = Path(tmpdir) / "active_tracks.json"
        dw.STATE_DIR = Path(tmpdir)
        yield
        dw.STATE_PATH = original


class TestRegisterTrack:
    def test_register_creates_entry(self):
        register_track("test-track", "Test track", "telegram_chat")
        tracks = get_active_tracks()
        assert len(tracks) == 1
        assert tracks[0]["id"] == "test-track"
        assert tracks[0]["label"] == "Test track"
        assert tracks[0]["track_type"] == "telegram_chat"
        assert tracks[0]["status"] == "running"

    def test_register_replaces_existing(self):
        register_track("same-id", "First", "telegram_chat")
        register_track("same-id", "Second", "email_poller")
        tracks = get_active_tracks()
        assert len(tracks) == 1
        assert tracks[0]["label"] == "Second"
        assert tracks[0]["track_type"] == "email_poller"

    def test_register_with_metadata(self):
        register_track(
            "t1", "With meta", "telegram_chat", metadata={"thread_id": "2744"}
        )
        tracks = get_active_tracks()
        assert tracks[0]["metadata"]["thread_id"] == "2744"


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self):
        register_track("hb-track", "Heartbeat test", "followup_monitor")
        # Force a known old timestamp by writing directly
        import app.deploy_watcher as dw

        state = json.loads(dw.STATE_PATH.read_text())
        old_ts = "2020-01-01T00:00:00Z"
        state["tracks"][0]["last_heartbeat"] = old_ts
        dw.STATE_PATH.write_text(json.dumps(state))
        heartbeat("hb-track")
        t2 = get_active_tracks()[0]["last_heartbeat"]
        assert t2 > old_ts  # New timestamp is after the forced old one
        assert t2 != old_ts  # Actually updated

    def test_heartbeat_unknown_track_does_not_error(self):
        # Should not raise
        heartbeat("nonexistent")


class TestUnregisterTrack:
    def test_unregister_removes_track(self):
        register_track("remove-me", "Remove test", "telegram_chat")
        assert len(get_active_tracks()) == 1
        unregister_track("remove-me")
        assert len(get_active_tracks()) == 0

    def test_unregister_unknown_track_does_not_error(self):
        unregister_track("nonexistent")


class TestCanDeploy:
    def test_can_deploy_when_no_tracks(self):
        ok, blocking = can_deploy()
        assert ok is True
        assert blocking == []

    def test_can_deploy_when_track_idle(self):
        register_track("old-track", "Old", "telegram_chat")
        # Simulate an old heartbeat by writing a stale timestamp
        import app.deploy_watcher as dw

        state = json.loads(dw.STATE_PATH.read_text())
        state["tracks"][0]["last_heartbeat"] = "2020-01-01T00:00:00Z"
        dw.STATE_PATH.write_text(json.dumps(state))
        ok, blocking = can_deploy()
        assert ok is True  # Exceeded max duration

    def test_can_deploy_blocked_by_active_track(self):
        register_track("active", "Active chat", "telegram_chat")
        ok, blocking = can_deploy()
        assert ok is False
        assert len(blocking) == 1
        assert blocking[0]["id"] == "active"

    def test_force_bypasses_all_checks(self):
        register_track("active", "Active chat", "telegram_chat")
        ok, blocking = can_deploy(force=True)
        assert ok is True
        assert blocking == []

    def test_multiple_blocking_tracks(self):
        register_track("t1", "Track 1", "telegram_chat")
        register_track("t2", "Track 2", "followup_monitor")
        ok, blocking = can_deploy()
        assert ok is False
        assert len(blocking) == 2

    def test_mixed_active_and_stale(self):
        register_track("active", "Active", "telegram_chat")
        register_track("stale", "Stale", "followup_monitor")
        # Make the stale one old
        import app.deploy_watcher as dw

        state = json.loads(dw.STATE_PATH.read_text())
        for t in state["tracks"]:
            if t["id"] == "stale":
                t["last_heartbeat"] = "2020-01-01T00:00:00Z"
        dw.STATE_PATH.write_text(json.dumps(state))
        ok, blocking = can_deploy()
        assert ok is False
        assert len(blocking) == 1
        assert blocking[0]["id"] == "active"


class TestGetSystemStatus:
    def test_returns_status_dict(self):
        status = get_system_status()
        assert "can_deploy" in status
        assert "active_tracks" in status
        assert "total_tracks" in status
        assert "checked_at" in status

    def test_includes_blocking_tracks(self):
        register_track("active", "Active", "telegram_chat")
        status = get_system_status()
        assert status["can_deploy"] is False
        assert len(status["blocking_tracks"]) == 1
