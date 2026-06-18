"""Tests: /vault/api/system-status reads tracks from the deploy_watcher registry.

The vault status page reports active tracks + deploy readiness from the same
registry main.py writes chat-turn tracks to (app.deploy_watcher), not a separate
module. These tests pin that wiring and the response shape.
"""

from __future__ import annotations

import asyncio


def test_system_status_no_tracks(monkeypatch):
    from app import vault_routes

    monkeypatch.setattr("app.deploy_watcher.get_active_tracks", lambda: [])
    monkeypatch.setattr("app.deploy_watcher.can_deploy", lambda force=False: (True, []))

    result = asyncio.run(vault_routes.get_system_status())

    result_expected = result.copy()
    assert result_expected.pop("commit_hash", None) is not None  # present
    assert result_expected == {"can_deploy": True, "total_tracks": 0, "active_tracks": []}


def test_system_status_reports_active_track(monkeypatch):
    from app import vault_routes

    track = {
        "id": "telegram:2744",
        "label": "chat turn",
        "track_type": "chat_turn",
        "status": "running",
        "started_at": "2026-06-16T00:00:00+00:00",
        "last_heartbeat": "2026-06-16T00:00:00+00:00",
        "expected_max_duration_s": 600,
    }
    monkeypatch.setattr("app.deploy_watcher.get_active_tracks", lambda: [track])
    monkeypatch.setattr(
        "app.deploy_watcher.can_deploy", lambda force=False: (False, [track])
    )

    result = asyncio.run(vault_routes.get_system_status())

    assert result["can_deploy"] is False
    assert result["total_tracks"] == 1
    entry = result["active_tracks"][0]
    assert entry["label"] == "chat turn"
    assert entry["track_type"] == "chat_turn"
    assert entry["status"] == "running"
    assert entry["max_duration_s"] == 600
    assert entry["elapsed_s"] >= 0
