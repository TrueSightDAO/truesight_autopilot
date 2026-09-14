"""Regression tests for the '_save_pending' off-thread event-loop crash.

Live incident (thread 29384, 2026-09-14 12:55:32 UTC): the
warmup-conversion-30day-readout follow-up turn crashed with
``RuntimeError: no running event loop``:

    followup_loop._spin_sophia_turn -> main._chat_blocking_turn
      -> _run_tool -> asyncio.to_thread(_run_tool_sync)
        -> _add_pending -> _save_pending
          -> asyncio.create_task(_sync_pending_to_github)   # <-- boom

``_run_tool_sync`` runs in a worker thread (``asyncio.to_thread``), which has
no running event loop, so the bare ``asyncio.create_task`` raised. The
safety net correctly left the follow-up OPEN, but the tool call itself died.

These tests pin the fix: ``_save_pending`` must never raise and must still
synchronise from a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import threading

import app.main as main


def test_save_pending_does_not_raise_from_worker_thread(tmp_path, monkeypatch):
    """Reproduces the exact incident: call _save_pending from a thread."""
    monkeypatch.setattr(main, "SESSION_LOG_DIR", tmp_path)
    calls: list = []

    async def _fake_sync(public_key, items):
        calls.append((public_key, list(items)))

    monkeypatch.setattr(main, "_sync_pending_to_github", _fake_sync)

    # Drive a real event loop in the MAIN thread and capture it, mirroring
    # how lifespan() sets _MAIN_LOOP at startup.
    async def _driver():
        main._MAIN_LOOP = asyncio.get_running_loop()

        err: list[BaseException] = []

        def _worker():
            try:
                main._save_pending("gov-key", [{"title": "x"}])
            except BaseException as e:  # noqa: BLE001 - we assert it's empty
                err.append(e)

        t = threading.Thread(target=_worker)
        t.start()
        # Give the cross-thread schedule a tick to run on this loop.
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.01)
        t.join(timeout=2)
        assert err == [], f"_save_pending raised from worker thread: {err!r}"

    asyncio.run(_driver())
    # File written, and the sync was dispatched (not silently dropped).
    assert list(tmp_path.glob("pending_*.json"))
    assert calls, "pending sync was never dispatched from the worker thread"


def test_save_pending_writes_local_file_and_schedules(tmp_path, monkeypatch):
    """Happy path: local file always written even with no captured loop."""
    monkeypatch.setattr(main, "SESSION_LOG_DIR", tmp_path)
    main._MAIN_LOOP = None
    calls: list = []

    async def _fake_sync(public_key, items):
        calls.append((public_key, list(items)))

    monkeypatch.setattr(main, "_sync_pending_to_github", _fake_sync)

    main._save_pending("gov-key-2", [{"title": "y"}])

    files = list(tmp_path.glob("pending_*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == [{"title": "y"}]
    assert calls and calls[0][1] == [{"title": "y"}]


def test_schedule_pending_sync_never_raises_without_any_loop(monkeypatch):
    """Defensive: no captured loop and no running loop -> still no raise."""
    main._MAIN_LOOP = None

    async def _fake_sync(public_key, items):
        return None

    monkeypatch.setattr(main, "_sync_pending_to_github", _fake_sync)
    # Called from a bare thread with no loop anywhere on the main thread yet.
    err: list[BaseException] = []

    def _worker():
        try:
            main._schedule_pending_sync("k", [])
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=2)
    assert err == []
