"""Unit tests for the Discord gateway RESUME + backoff-reset fix.

Covers PR0 of plans/DISCORD_GATEWAY_RESUME_BACKOFF_FIX_PLAN.md. Hermetic: a fake
websocket stands in for ``websockets.connect``, so there is no network and no
real gateway. Async tests use ``@pytest.mark.asyncio``, which this repo's
``tests/conftest.py`` hook runs via ``asyncio.run`` (pytest-asyncio is
intentionally not a dependency).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app import discord_adapter as da


class _FakeWS:
    """Minimal async-context-manager websocket double."""

    def __init__(self, hello, events, close_code=None):
        self._hello = hello
        self._events = list(events)
        self.close_code = close_code
        self.sent: list[dict] = []
        self.url = None

    async def recv(self):
        return json.dumps(self._hello)

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return json.dumps(self._events.pop(0))


class _FakeConnect:
    """Callable standing in for ``websockets.connect`` (returns an async CM)."""

    def __init__(self, ws):
        self.ws = ws

    def __call__(self, url, **kwargs):
        self.ws.url = url
        return self

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


_HELLO = {"op": 10, "d": {"heartbeat_interval": 45000}}


def _install_ws(monkeypatch, events, close_code=None):
    ws = _FakeWS(_HELLO, events, close_code=close_code)
    monkeypatch.setattr(websockets, "connect", _FakeConnect(ws))
    monkeypatch.setattr(da, "get_token", lambda: "test-token")
    return ws


def _identify_or_resume(ws):
    """Return the first op-2/op-6 frame the client sent (skips heartbeats)."""
    for frame in ws.sent:
        if frame.get("op") in (2, 6):
            return frame
    raise AssertionError("no IDENTIFY/RESUME frame sent")


# ── IDENTIFY vs RESUME ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identify_when_session_empty(monkeypatch):
    ws = _install_ws(monkeypatch, [])
    await da._gateway_once("wss://gw", {}, set(), "KEY", "1", "42", lambda d: None)
    frame = _identify_or_resume(ws)
    assert frame["op"] == 2
    assert frame["d"]["intents"] == da.GATEWAY_INTENTS


@pytest.mark.asyncio
async def test_resume_when_session_populated(monkeypatch):
    ws = _install_ws(monkeypatch, [])
    session = {"session_id": "abc", "seq": 7}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    frame = _identify_or_resume(ws)
    assert frame["op"] == 6
    assert frame["d"]["session_id"] == "abc"
    assert frame["d"]["seq"] == 7


@pytest.mark.asyncio
async def test_resume_prefers_resume_url(monkeypatch):
    ws = _install_ws(monkeypatch, [])
    session = {"session_id": "abc", "seq": 3, "resume_url": "wss://resume.example/q"}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert ws.url == "wss://resume.example/q"


# ── READY / RESUMED / INVALID_SESSION state transitions ─────────────────


@pytest.mark.asyncio
async def test_ready_populates_session_and_resume_url(monkeypatch):
    ready = {
        "op": 0,
        "t": "READY",
        "s": 1,
        "d": {"session_id": "sess1", "resume_gateway_url": "wss://resume.example"},
    }
    _install_ws(monkeypatch, [ready])
    session: dict = {}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session["session_id"] == "sess1"
    assert session["seq"] == 1
    assert session["resume_url"] == "wss://resume.example" + da._GATEWAY_QUERY


@pytest.mark.asyncio
async def test_ready_without_resume_gateway_url(monkeypatch):
    ready = {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "sess1"}}
    _install_ws(monkeypatch, [ready])
    session: dict = {}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session["session_id"] == "sess1"
    assert "resume_url" not in session


@pytest.mark.asyncio
async def test_resumed_keeps_session(monkeypatch):
    resumed = {"op": 0, "t": "RESUMED", "s": 5, "d": {}}
    _install_ws(monkeypatch, [resumed])
    session = {"session_id": "abc", "seq": 4, "resume_url": "wss://r"}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session["session_id"] == "abc"


@pytest.mark.asyncio
async def test_invalid_session_false_clears_session(monkeypatch):
    _install_ws(monkeypatch, [{"op": 9, "d": False}])
    session = {"session_id": "abc", "seq": 4, "resume_url": "wss://r"}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session == {}


@pytest.mark.asyncio
async def test_invalid_session_true_keeps_session(monkeypatch):
    _install_ws(monkeypatch, [{"op": 9, "d": True}])
    session = {"session_id": "abc", "seq": 4}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session["session_id"] == "abc"
    assert session["seq"] == 4


@pytest.mark.asyncio
async def test_unresumable_close_code_clears_session(monkeypatch):
    _install_ws(monkeypatch, [], close_code=4007)
    session = {"session_id": "abc", "seq": 4, "resume_url": "wss://r"}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session == {}


@pytest.mark.asyncio
async def test_normal_close_code_keeps_session(monkeypatch):
    _install_ws(monkeypatch, [], close_code=1000)
    session = {"session_id": "abc", "seq": 4}
    await da._gateway_once("wss://gw", session, set(), "KEY", "1", "42", lambda d: None)
    assert session["session_id"] == "abc"


# ── backoff reset-on-stable-connection ──────────────────────────────────


class _StopLoop(Exception):
    pass


def _drive_loop(monkeypatch, step, stop_after):
    """Run _gateway_loop, advancing a fake clock by ``step`` per time() call.

    ``_gateway_once`` always raises; ``asyncio.sleep`` records its argument and
    raises ``_StopLoop`` after ``stop_after`` sleeps to break out. Returns the
    list of slept backoff values.
    """
    tick = {"i": 0}

    def fake_time():
        value = tick["i"] * step
        tick["i"] += 1
        return value

    slept: list[float] = []

    async def fake_once(*args, **kwargs):
        raise RuntimeError("boom")

    async def fake_sleep(value):
        slept.append(value)
        if len(slept) >= stop_after:
            raise _StopLoop

    monkeypatch.setattr(da.time, "time", fake_time)
    monkeypatch.setattr(da, "_gateway_url", lambda: "wss://gw")
    monkeypatch.setattr(da, "_gateway_once", fake_once)
    monkeypatch.setattr(da.asyncio, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        asyncio.run(da._gateway_loop(set(), "KEY", "1", "42", lambda d: None))
    return slept


def test_backoff_climbs_on_fast_failures(monkeypatch):
    # No time passes between connect and failure (< 60s) -> backoff must climb.
    slept = _drive_loop(monkeypatch, step=0.0, stop_after=2)
    assert slept == [5.0, 10.0]


def test_backoff_resets_after_stable_connection(monkeypatch):
    # Each connection lasted 61s (>= 60s) -> backoff resets to 5s every time.
    slept = _drive_loop(monkeypatch, step=61.0, stop_after=2)
    assert slept == [5.0, 5.0]
