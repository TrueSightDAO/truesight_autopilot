"""Mid-task acknowledgment: a message sent while a topic is busy must be
acknowledged + queued, not silently blocked. Different/idle topics: no ack.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    import app.telegram_adapter as ta
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.telegram_adapter import unavailable: {exc}", allow_module_level=True
    )


def test_ack_sent_when_topic_busy(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        ta,
        "send_message",
        lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)),
    )

    lock = ta._thread_dispatch_lock(-100, 5)
    lock.acquire()  # simulate an in-flight turn
    try:
        ta._ack_queued_if_busy(-100, 5, lock)
    finally:
        lock.release()

    assert len(sent) == 1
    chat_id, text, thread_id = sent[0]
    assert chat_id == -100 and thread_id == 5
    assert "queue" in text.lower()


def test_no_ack_when_topic_idle(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        ta,
        "send_message",
        lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)),
    )

    lock = ta._thread_dispatch_lock(-100, 6)  # not held
    ta._ack_queued_if_busy(-100, 6, lock)
    assert sent == []


def test_ack_includes_live_snapshot_when_available(monkeypatch):
    """When busy AND a live-progress snapshot is fetchable over HTTP, the ack
    carries it. Regression for the 2026-06-17 cross-process bug: the snapshot
    must come from _fetch_progress_snapshot (HTTP to the brain), not an
    in-process dict that is always empty in the adapter process."""
    sent: list[tuple] = []
    monkeypatch.setattr(
        ta,
        "send_message",
        lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)),
    )
    monkeypatch.setattr(
        ta,
        "_fetch_progress_snapshot",
        lambda session_id, public_key: "round 3, 42s — running `open_fix_pr`",
    )

    lock = ta._thread_dispatch_lock(-100, 7)
    lock.acquire()
    try:
        ta._ack_queued_if_busy(-100, 7, lock, session_id="tg:-100:7", public_key="PK")
    finally:
        lock.release()

    assert len(sent) == 1
    text = sent[0][1]
    assert "queue" in text.lower()
    assert "Right now:" in text and "open_fix_pr" in text


def test_ack_plain_when_snapshot_unavailable(monkeypatch):
    """No snapshot (nothing running / fetch failed) → plain ack, no 'Right now:'."""
    sent: list[tuple] = []
    monkeypatch.setattr(
        ta,
        "send_message",
        lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)),
    )
    monkeypatch.setattr(
        ta, "_fetch_progress_snapshot", lambda session_id, public_key: None
    )

    lock = ta._thread_dispatch_lock(-100, 8)
    lock.acquire()
    try:
        ta._ack_queued_if_busy(-100, 8, lock, session_id="tg:-100:8", public_key="PK")
    finally:
        lock.release()

    assert len(sent) == 1
    assert "Right now:" not in sent[0][1]
