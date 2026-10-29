"""Mid-task acknowledgment on Discord: a message posted into a channel that is
already running a turn must be acknowledged + queued, not silently blocked.

Discord had NO equivalent of Telegram's ``_ack_queued_if_busy`` -- a message
posted while a turn was live just blocked on the channel lock, so from the
governor's side it looked dropped. This closes that asymmetry.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    import app.discord_adapter as da
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.discord_adapter import unavailable: {exc}", allow_module_level=True
    )


def _capture(monkeypatch) -> list[tuple]:
    sent: list[tuple] = []

    def _send(channel_id, text, *a, **k):
        sent.append((channel_id, text))

    monkeypatch.setattr(da, "send_message", _send)
    return sent


def test_ack_sent_when_channel_busy(monkeypatch):
    sent = _capture(monkeypatch)
    lock = da._channel_dispatch_lock("923008087315587072")
    lock.acquire()  # simulate an in-flight turn
    try:
        da._ack_queued_if_busy(lock, "923008087315587072")
    finally:
        lock.release()

    assert len(sent) == 1
    channel_id, text = sent[0]
    assert channel_id == "923008087315587072"
    assert "queue" in text.lower()


def test_no_ack_when_channel_idle(monkeypatch):
    sent = _capture(monkeypatch)
    lock = da._channel_dispatch_lock("923008087315587073")  # not held
    da._ack_queued_if_busy(lock, "923008087315587073")
    assert sent == []


def test_ack_includes_live_snapshot_when_available(monkeypatch):
    """The snapshot must come from ``_fetch_progress_snapshot`` (HTTP to the
    brain), never an in-process dict -- the adapter is a separate process from
    the brain, so a local dict is always empty."""
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        da,
        "_fetch_progress_snapshot",
        lambda session_id, public_key: "round 3, 42s -- running `open_fix_pr`",
    )
    lock = da._channel_dispatch_lock("923008087315587074")
    lock.acquire()
    try:
        da._ack_queued_if_busy(lock, "923008087315587074", "sess-1", "pk-1")
    finally:
        lock.release()

    assert len(sent) == 1
    assert "open_fix_pr" in sent[0][1]


def test_ack_without_snapshot_still_acknowledges(monkeypatch):
    """A failed progress fetch must still produce a plain ack -- progress is
    best-effort and never blocks the acknowledgment itself."""
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        da, "_fetch_progress_snapshot", lambda session_id, public_key: None
    )
    lock = da._channel_dispatch_lock("923008087315587075")
    lock.acquire()
    try:
        da._ack_queued_if_busy(lock, "923008087315587075", "sess-2", "pk-2")
    finally:
        lock.release()

    assert len(sent) == 1
    assert "queue" in sent[0][1].lower()


def test_ack_failure_never_raises(monkeypatch):
    """A Discord hiccup while acknowledging must not break the dispatch path --
    an unacknowledged message is bad, a crashed adapter is worse."""

    def _boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(da, "send_message", _boom)
    lock = da._channel_dispatch_lock("923008087315587076")
    lock.acquire()
    try:
        da._ack_queued_if_busy(lock, "923008087315587076")  # must not raise
    finally:
        lock.release()
