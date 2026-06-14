"""Graceful brain-restart handling: a redeploy shows a clear indicator, not Errno 111."""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    from app import telegram_adapter as ta
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"telegram_adapter import unavailable: {exc}", allow_module_level=True)


def test_deploy_in_progress_tracks_marker(monkeypatch, tmp_path):
    marker = tmp_path / ".autopilot_deployed"
    monkeypatch.setattr(ta, "_DEPLOY_MARKER", str(marker))
    assert ta._deploy_in_progress() is False
    marker.write_text("commit")
    assert ta._deploy_in_progress() is True


def test_message_names_redeploy_when_marker_present(monkeypatch, tmp_path):
    marker = tmp_path / ".autopilot_deployed"
    monkeypatch.setattr(ta, "_DEPLOY_MARKER", str(marker))
    # brain down for an unknown reason → generic "restarting"
    assert "restart" in ta._brain_unavailable_message().lower()
    # a redeploy is underway → name it
    marker.write_text("commit")
    assert "redeploy" in ta._brain_unavailable_message().lower()


def test_wait_for_brain_returns_false_fast_when_down(monkeypatch):
    # No real HTTP / no sleeping in the test — every probe "fails" instantly.
    monkeypatch.setattr(
        ta.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(Exception("refused"))
    )
    monkeypatch.setattr(ta.time, "sleep", lambda *_: None)
    assert ta._wait_for_brain(max_attempts=3, backoff=0) is False


def test_wait_for_brain_true_when_up(monkeypatch):
    class _R:
        status_code = 200

    monkeypatch.setattr(ta.httpx, "get", lambda *a, **k: _R())
    assert ta._wait_for_brain() is True
