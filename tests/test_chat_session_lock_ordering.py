"""Regression tests for /chat's session-lock ordering (2026-07-21).

Guards against the bug fixed here: previously /chat loaded history, ran
role-detection branches, and appended + persisted the incoming user message
BEFORE acquiring the per-session lock (`_session_lock`) — a window where a
second same-thread request could interleave its own read/append/write with
an in-flight turn's. That's the exact class of corruption `_session_lock`
exists to prevent (its docstring cites "the race that bricked threads 3 and
780"). `/chat-blocking` already acquired the lock before touching history at
all; this brings `/chat` in line with it — the lock now wraps the ENTIRE
handler body, not just the final turn execution.

Found while scoping a governor question about whether pinging an
already-busy Sophia thread is safe. That specific case (ping_sophia ->
/chat-blocking) was already safe; this was a narrower, related gap in /chat
(the Telegram adapter's own dispatch path) uncovered during that scoping.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import httpx
import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.roles import ROLES
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


def _wire_common(monkeypatch):
    """Bypass auth + role-detection + side-channel bookkeeping so the test
    exercises only the lock-ordering property, not unrelated subsystems."""
    monkeypatch.setattr(m, "verify_jwt", lambda request: "testpublickey0123456789")
    monkeypatch.setattr(m, "find_role_in_history", lambda history: ROLES["general"])
    monkeypatch.setattr(m, "_gov_name_for_key", lambda pk: None)
    monkeypatch.setattr(m, "_auto_name_session", lambda *a, **k: None)
    monkeypatch.setattr(m, "_register_chat_turn_track", lambda *a, **k: None)
    monkeypatch.setattr(m, "_unregister_chat_turn_track", lambda *a, **k: None)


async def _post_chat(message: str, session_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/chat",
            json={"message": message},
            headers={"Authorization": "Bearer x", "X-Session-Id": session_id},
        )


def test_history_loaded_only_after_lock_acquired(monkeypatch):
    """Core regression guard: _load_or_create_session must not run until this
    session's _session_lock is already held."""
    _wire_common(monkeypatch)
    session_id = "lock-order-test-1"

    observations: list[tuple[str, bool]] = []
    real_load = m._load_or_create_session

    def _spy_load(sid):
        lock = m._session_lock(sid)
        observations.append(("load", lock.locked()))
        return real_load(sid)

    monkeypatch.setattr(m, "_load_or_create_session", _spy_load)

    async def _fake_stream_chat(*args, **kwargs):
        yield m._sse_event("done", {"response": "ok"})

    monkeypatch.setattr(m, "_stream_chat", _fake_stream_chat)

    resp = asyncio.run(_post_chat("hello", session_id))
    assert resp.status_code == 200
    assert observations == [("load", True)], observations


def test_second_request_blocks_until_first_releases_lock(monkeypatch):
    """A second same-thread request must not load/mutate history until the
    in-flight turn's lock is released — the actual race this fix closes."""
    _wire_common(monkeypatch)
    session_id = "lock-order-test-2"

    order: list[str] = []
    turn1_entered = asyncio.Event()
    release_turn1 = asyncio.Event()
    call_count = {"n": 0}

    real_load = m._load_or_create_session

    def _spy_load(sid):
        # sid is _session_key()'s output: f"{public_key[:20]}:{X-Session-Id}",
        # not the bare header value — match by suffix, not equality.
        if sid.endswith(session_id):
            order.append("load")
        return real_load(sid)

    monkeypatch.setattr(m, "_load_or_create_session", _spy_load)

    async def _fake_stream_chat(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            order.append("turn1-start")
            turn1_entered.set()
            await release_turn1.wait()
            order.append("turn1-end")
        else:
            order.append("turn2-start")
        yield m._sse_event("done", {"response": "ok"})

    monkeypatch.setattr(m, "_stream_chat", _fake_stream_chat)

    async def _run():
        task1 = asyncio.create_task(_post_chat("first", session_id))
        await turn1_entered.wait()  # turn 1 is inside _stream_chat, holding the lock

        task2 = asyncio.create_task(_post_chat("second", session_id))
        await asyncio.sleep(0.05)  # give task2 a chance to run if it (incorrectly) could
        # Task2 must be blocked on the lock: no second "load" yet.
        assert order.count("load") == 1, order

        release_turn1.set()
        r1 = await task1
        r2 = await task2
        assert r1.status_code == 200
        assert r2.status_code == 200

    asyncio.run(_run())
    assert order == ["load", "turn1-start", "turn1-end", "load", "turn2-start"], order


def test_role_menu_early_return_still_covered_by_lock(monkeypatch):
    """The role-selection early-return path (no role, brand-new session, no
    default configured) must also run under the lock — it used to run
    entirely before the lock was ever acquired."""
    monkeypatch.setattr(m, "verify_jwt", lambda request: "testpublickey0123456789")
    monkeypatch.setattr(m, "find_role_in_history", lambda history: None)
    monkeypatch.setattr(m, "get_default_role", lambda: None)
    session_id = "lock-order-test-3"

    observations: list[bool] = []
    real_load = m._load_or_create_session

    def _spy_load(sid):
        observations.append(m._session_lock(sid).locked())
        return real_load(sid)

    monkeypatch.setattr(m, "_load_or_create_session", _spy_load)

    resp = asyncio.run(_post_chat("hello", session_id))
    assert resp.status_code == 200
    assert observations == [True], observations
    assert "Role" in resp.text or "role" in resp.text
