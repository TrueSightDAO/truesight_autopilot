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
    pytest.skip(f"app.telegram_adapter import unavailable: {exc}", allow_module_level=True)


def test_ack_sent_when_topic_busy(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(ta, "send_message", lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)))

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
    monkeypatch.setattr(ta, "send_message", lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)))

    lock = ta._thread_dispatch_lock(-100, 6)  # not held
    ta._ack_queued_if_busy(-100, 6, lock)
    assert sent == []
