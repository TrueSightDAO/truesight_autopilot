"""PR2 — adapter per-thread dispatch serialization.

The handler pool runs messages concurrently. The per-(chat,thread) dispatch lock
must serialize turns within a topic (so rapid-fire messages queue instead of
racing the transcript) while different topics run in parallel. Attachment prep
runs before the lock and stays parallel.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    import app.telegram_adapter as ta
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.telegram_adapter import unavailable: {exc}", allow_module_level=True
    )


def test_lock_identity():
    ta._thread_dispatch_locks.clear()
    a1 = ta._thread_dispatch_lock(-100, 5)
    a2 = ta._thread_dispatch_lock(-100, 5)
    b = ta._thread_dispatch_lock(-100, 6)
    c = ta._thread_dispatch_lock(-100, None)  # bare topic → key :0
    assert a1 is a2  # same topic → same lock
    assert a1 is not b  # different topic → different lock
    assert c is ta._thread_dispatch_lock(-100, 0)  # None and 0 collapse


def _run_two(sid_args_a, sid_args_b):
    """Run two 'turns' concurrently, each holding its topic lock for 50ms while
    recording enter/exit. Returns the event order."""
    events: list[str] = []
    gate = threading.Barrier(2)

    def turn(tag, chat_id, thread_id):
        gate.wait()
        with ta._thread_dispatch_lock(chat_id, thread_id):
            events.append(f"enter-{tag}")
            time.sleep(0.05)
            events.append(f"exit-{tag}")

    t1 = threading.Thread(target=turn, args=("A", *sid_args_a))
    t2 = threading.Thread(target=turn, args=("B", *sid_args_b))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    return events


def test_same_topic_serializes():
    ta._thread_dispatch_locks.clear()
    events = _run_two((-100, 7), (-100, 7))
    # one fully completes before the other enters
    assert events in (
        ["enter-A", "exit-A", "enter-B", "exit-B"],
        ["enter-B", "exit-B", "enter-A", "exit-A"],
    ), events


def test_different_topics_run_concurrently():
    ta._thread_dispatch_locks.clear()
    events = _run_two((-100, 7), (-100, 8))
    # both enter before either exits → parallel across topics
    assert events[0].startswith("enter") and events[1].startswith("enter"), events
