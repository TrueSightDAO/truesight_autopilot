"""Regression: live chat turns must appear in deploy_watcher's track registry.

Bug (2026-06-14): the vault status panel (sophia.truesight.me/vault/, served by
the standalone vault_app on port 8002 — a separate process) reads
deploy_watcher.active_tracks.json. Only aws_monitor/email_poller registered
tracks; the chat path never did, so live conversations showed "Active tracks: 0"
and the panel's can_deploy gate couldn't see them. main now mirrors the
_active_streams turn lifecycle into the file-based registry.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# Point the deploy_watcher file registry at a temp dir BEFORE importing.
os.environ["DEPLOY_WATCHER_STATE_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app import deploy_watcher as dw
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def test_chat_turn_appears_then_clears():
    sid = "tg:-100:3926"
    assert all(t["id"] != sid for t in dw.get_active_tracks())

    m._register_chat_turn_track(sid, "Gary Teh")
    tracks = {t["id"]: t for t in dw.get_active_tracks()}
    assert sid in tracks  # the panel would now show this live turn
    assert tracks[sid]["track_type"] == "telegram_chat"

    m._unregister_chat_turn_track(sid)
    assert all(t["id"] != sid for t in dw.get_active_tracks())


def test_register_is_best_effort(monkeypatch):
    """A registry failure must never propagate out of a turn."""

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(m, "_dw_register_track", _boom)
    # Must not raise:
    m._register_chat_turn_track("tg:-100:9", "Gary Teh")


def test_unregister_is_best_effort(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(m, "_dw_unregister_track", _boom)
    m._unregister_chat_turn_track("tg:-100:9")  # must not raise
