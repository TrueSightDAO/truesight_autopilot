"""PR1 — one writer / one executor per thread.

The per-session async lock must serialize turns for the SAME session (so two
same-thread requests can't interleave their transcript writes — the race that
bricked threads 3 and 780) while letting DIFFERENT sessions run concurrently.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.main import unavailable in this env: {exc}", allow_module_level=True
    )


def _dangling(history):
    bad = []
    n = len(history)
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            ids = {tc["id"] for tc in msg["tool_calls"]}
            seen = set()
            j = i + 1
            while j < n and history[j].get("role") == "tool":
                seen.add(history[j].get("tool_call_id"))
                j += 1
            if not ids.issubset(seen):
                bad.append(i)
    return bad


def test_lock_identity():
    m._session_locks.clear()

    async def _main():
        a1 = m._session_lock("tg:1:1")
        a2 = m._session_lock("tg:1:1")
        b = m._session_lock("tg:1:2")
        assert a1 is a2  # same session → same lock
        assert a1 is not b  # different session → different lock

    asyncio.run(_main())


def test_same_session_serializes():
    m._session_locks.clear()
    events: list[str] = []

    async def turn(tag):
        async with m._session_lock("tg:X:1"):
            events.append(f"enter-{tag}")
            await asyncio.sleep(0.02)
            events.append(f"exit-{tag}")

    async def _main():
        await asyncio.wait_for(asyncio.gather(turn("A"), turn("B")), timeout=5)

    asyncio.run(_main())
    # one turn fully completes before the other enters — no interleave
    assert events in (
        ["enter-A", "exit-A", "enter-B", "exit-B"],
        ["enter-B", "exit-B", "enter-A", "exit-A"],
    )


def test_different_sessions_run_concurrently():
    m._session_locks.clear()
    events: list[str] = []

    async def turn(sid, tag):
        async with m._session_lock(sid):
            events.append(f"enter-{tag}")
            await asyncio.sleep(0.02)
            events.append(f"exit-{tag}")

    async def _main():
        await asyncio.wait_for(
            asyncio.gather(turn("tg:X:1", "A"), turn("tg:X:2", "B")), timeout=5
        )

    asyncio.run(_main())
    # both enter before either exits → genuinely concurrent
    assert events[0].startswith("enter") and events[1].startswith("enter")


def test_lock_prevents_dangling_tool_calls():
    """Reproduce the incident's critical section under the lock and assert the
    persisted transcript is well-formed. Two concurrent turns each do:
    append(assistant tool_calls) → await(tool runs) → append(tool result).
    Without the lock this interleaves to [a, a, t, t] (dangling); with it, each
    turn completes atomically → [a, t, a, t]."""
    m._session_locks.clear()
    session: list[dict] = []  # the shared single-process transcript

    async def turn(call_id):
        async with m._session_lock("tg:race:1"):
            session.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                }
            )
            await asyncio.sleep(0.01)  # tool-execution window where the race struck
            session.append({"role": "tool", "tool_call_id": call_id, "content": "ok"})

    async def _main():
        await asyncio.wait_for(asyncio.gather(turn("c1"), turn("c2")), timeout=5)

    asyncio.run(_main())
    assert _dangling(session) == [], f"lock failed to serialize: {session}"
    assert len(session) == 4
