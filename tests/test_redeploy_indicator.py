"""Graceful brain-restart handling: a redeploy shows a clear indicator, not Errno 111.

Since 2026-09-02 (thread-19615) the adapter must also distinguish a DOWN brain
(connection refused) from a BUSY brain (probe timeout) instead of the blanket
"briefly restarting" text — and must LOG every failed health probe so there is an
evidence trail.
"""

from __future__ import annotations

import logging
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


def test_wait_for_brain_records_and_logs_probe_failure(monkeypatch, caplog):
    # The 2026-09-02 thread-19615 regression: a failed health probe left NO log
    # line and no recorded reason, so the adapter could only say "restarting".
    def _boom(*a, **k):
        raise ConnectionError("[Errno 111] Connection refused")

    monkeypatch.setattr(ta.httpx, "get", _boom)
    monkeypatch.setattr(ta.time, "sleep", lambda *_: None)
    ta._LAST_BRAIN_PROBE_ERROR = ""
    with caplog.at_level(logging.WARNING, logger=ta.logger.name):
        assert ta._wait_for_brain(max_attempts=2, backoff=0) is False
    assert "errno 111" in ta._LAST_BRAIN_PROBE_ERROR.lower()
    assert "health probe" in caplog.text.lower()
    assert "attempt 1/2" in caplog.text


def test_wait_for_brain_clears_error_on_success(monkeypatch):
    class _R:
        status_code = 200

    ta._LAST_BRAIN_PROBE_ERROR = "ConnectError: old stale failure"
    monkeypatch.setattr(ta.httpx, "get", lambda *a, **k: _R())
    assert ta._wait_for_brain() is True
    assert ta._LAST_BRAIN_PROBE_ERROR == ""


def test_message_names_down_brain_when_connection_refused():
    # A refused connection means the brain process is DOWN, not "restarting".
    ta._LAST_BRAIN_PROBE_ERROR = "ConnectError: [Errno 111] Connection refused"
    msg = ta._brain_unavailable_message().lower()
    assert "down" in msg
    assert "connection refused" in msg
    assert not msg.startswith("⏳ sophia is briefly restarting")


def test_message_names_busy_brain_when_probe_timed_out():
    # A probe timeout means the brain is UP but unresponsive/busy.
    ta._LAST_BRAIN_PROBE_ERROR = "ReadTimeout: timed out after 5.0s"
    msg = ta._brain_unavailable_message().lower()
    assert "busy" in msg
    assert "down" not in msg
