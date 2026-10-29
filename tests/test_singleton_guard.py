"""PR -- cross-process singleton guard (``app/singleton_guard.py``).

Pins the contract that makes ``uvicorn --workers N`` safe for the nine
housekeeping loops started in ``app/main.py``'s ``lifespan()``:

* two workers racing the same name -> exactly one runs the loop;
* the loser logs one line, returns WITHOUT running, and closes the coroutine it
  was handed (no 'coroutine was never awaited' noise on the other 3 workers);
* a winner's claim survives past a single TTL window thanks to renewal, while
  an unrenewed claim of the same TTL does expire;
* finishing (or crashing) the loop releases the claim;
* Redis being unreachable degrades OPEN -- the loop still runs.

``FakeRedis`` emulates exactly the commands ``redis_claim`` issues (SET NX EX /
GET / EVAL) with real TTL bookkeeping, so no live Redis is needed.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import socket
import time

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import redis_claim, singleton_guard
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app imports unavailable: {exc}", allow_module_level=True)

NAME = "calendar_watchdog"
KEY = redis_claim.lock_key(singleton_guard.singleton_key(NAME))
MAIN_PATH = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"

#: The nine loops that must be singleton-guarded in lifespan().
LIFESPAN_SINGLETONS = (
    "email_watch",
    "email_poller",
    "aws_monitor",
    "_branch_janitor_loop",
    "_pending_janitor_loop",
    "_context_sync_loop",
    "followup_loop",
    "calendar_watchdog_loop",
    "_catalog_refresh_loop",
)


class FakeRedis:
    """Minimal in-memory stand-in for the Redis commands ``redis_claim`` uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}
        self.down = False
        self.calls: list[str] = []
        self.renew_count = 0

    def _expire_if_due(self, key: str) -> None:
        deadline = self.expires_at.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self.store.pop(key, None)
            self.expires_at.pop(key, None)

    def ttl_remaining(self, key: str) -> float | None:
        self._expire_if_due(key)
        if key not in self.store:
            return None
        return self.expires_at[key] - time.monotonic()

    def set(self, key, value, nx=False, ex=None):
        self.calls.append("set")
        if self.down:
            raise ConnectionError("redis unreachable")
        self._expire_if_due(key)
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.expires_at[key] = time.monotonic() + int(ex)
        else:
            self.expires_at.pop(key, None)
        return True

    def get(self, key):
        self.calls.append("get")
        if self.down:
            raise ConnectionError("redis unreachable")
        self._expire_if_due(key)
        return self.store.get(key)

    def eval(self, script, numkeys, key, *args):
        self.calls.append("eval")
        if self.down:
            raise ConnectionError("redis unreachable")
        self._expire_if_due(key)
        owner = args[0]
        if script is redis_claim._RELEASE_LUA:
            if self.store.get(key) == owner:
                self.store.pop(key, None)
                self.expires_at.pop(key, None)
                return 1
            return 0
        if script is redis_claim._RENEW_LUA:
            if self.store.get(key) == owner:
                self.renew_count += 1
                self.expires_at[key] = time.monotonic() + int(args[1])
                return 1
            return 0
        raise AssertionError("unexpected Lua script")


# --- the nine lifespan() loops are actually wrapped -------------------------
def test_all_nine_lifespan_loops_are_singleton_wrapped():
    src = MAIN_PATH.read_text(encoding="utf-8")
    for name in LIFESPAN_SINGLETONS:
        assert f'run_as_singleton("{name}"' in src, f"{name} is not singleton-guarded"
    assert "from .singleton_guard import run_as_singleton" in src


def test_default_worker_id_is_host_and_pid():
    assert singleton_guard.default_worker_id() == (
        f"{socket.gethostname()}:{os.getpid()}"
    )


def test_singleton_key_namespace():
    assert singleton_guard.singleton_key("x") == "singleton:x"
    assert KEY == f"lock:singleton:{NAME}"


# --- the race: exactly one winner ------------------------------------------
def test_two_workers_racing_same_name_exactly_one_runs(caplog):
    caplog.set_level("INFO")

    async def _scenario() -> FakeRedis:
        fake = FakeRedis()
        stop = asyncio.Event()
        ran: list[str] = []

        async def _body(who: str) -> None:
            ran.append(who)
            await stop.wait()

        coro_a = _body("w1")
        coro_b = _body("w2")
        task_a = asyncio.create_task(
            singleton_guard.run_as_singleton(
                NAME, coro_a, ttl=60, client=fake, worker_id="w1"
            )
        )
        task_b = asyncio.create_task(
            singleton_guard.run_as_singleton(
                NAME, coro_b, ttl=60, client=fake, worker_id="w2"
            )
        )
        await asyncio.sleep(0.05)

        assert len(ran) == 1, f"expected exactly one runner, got {ran!r}"
        assert sum(1 for t in (task_a, task_b) if t.done()) == 1, "loser must return"

        winner = ran[0]
        assert fake.store[KEY] == winner
        loser_coro = coro_b if winner == "w1" else coro_a
        assert inspect.getcoroutinestate(loser_coro) == inspect.CORO_CLOSED

        stop.set()
        await asyncio.gather(task_a, task_b)
        return fake

    fake = asyncio.run(_scenario())
    assert KEY not in fake.store, "winner must release on exit"
    assert "already owned by another worker" in caplog.text
    assert "-- skipping" in caplog.text


# --- renewal keeps the claim past a full TTL window ------------------------
def test_renewal_keeps_claim_alive_past_one_ttl_window():
    ttl = 2

    async def _scenario() -> FakeRedis:
        fake = FakeRedis()
        started = asyncio.Event()
        stop = asyncio.Event()

        async def _body() -> None:
            started.set()
            await stop.wait()

        task = asyncio.create_task(
            singleton_guard.run_as_singleton(
                NAME, _body(), ttl=ttl, client=fake, worker_id="w1"
            )
        )
        await started.wait()
        assert fake.store[KEY] == "w1"

        # Control: same TTL, no renewal -> this one MUST be gone after a window.
        fake.set("control:unrenewed", "x", nx=True, ex=ttl)

        await asyncio.sleep(ttl + 0.6)

        assert fake.store.get(KEY) == "w1", "renewal should keep our claim alive"
        assert fake.ttl_remaining(KEY) > 0
        assert fake.get("control:unrenewed") is None, (
            "control claim should have expired"
        )
        assert fake.renew_count >= 2, f"expected renewal, saw {fake.renew_count}"

        stop.set()
        await task
        return fake

    fake = asyncio.run(_scenario())
    assert KEY not in fake.store, "claim released after the loop finishes"


# --- finishing / crashing the loop releases the claim ---------------------
def test_loop_exception_is_logged_and_claim_released(caplog):
    caplog.set_level("INFO")

    async def _scenario() -> FakeRedis:
        fake = FakeRedis()

        async def _boom() -> None:
            raise RuntimeError("boom")

        await singleton_guard.run_as_singleton(
            NAME, _boom(), ttl=60, client=fake, worker_id="w1"
        )
        return fake

    fake = asyncio.run(_scenario())
    assert KEY not in fake.store
    assert "crashed -- releasing its claim" in caplog.text


# --- degrade open: Redis unreachable must NOT kill the loops --------------
def test_redis_unreachable_degrades_open_and_still_runs(caplog):
    caplog.set_level("INFO")

    async def _scenario() -> list[str]:
        fake = FakeRedis()
        fake.down = True
        ran: list[str] = []

        async def _body() -> None:
            ran.append("ran")

        await singleton_guard.run_as_singleton(
            NAME, _body(), ttl=60, client=fake, worker_id="w1"
        )
        return ran

    assert asyncio.run(_scenario()) == ["ran"]
    assert "running it in this worker anyway" in caplog.text


def test_renewal_failure_keeps_loop_running(caplog):
    caplog.set_level("INFO")
    ttl = 1

    async def _scenario() -> list[str]:
        fake = FakeRedis()
        started = asyncio.Event()
        stop = asyncio.Event()
        ran: list[str] = []

        async def _body() -> None:
            ran.append("started")
            started.set()
            await stop.wait()
            ran.append("finished")

        task = asyncio.create_task(
            singleton_guard.run_as_singleton(
                NAME, _body(), ttl=ttl, client=fake, worker_id="w1"
            )
        )
        await started.wait()
        fake.down = True  # Redis dies mid-flight
        await asyncio.sleep(0.5)
        assert not task.done(), "a failed renewal must not tear the loop down"
        fake.down = False
        stop.set()
        await task
        return ran

    assert asyncio.run(_scenario()) == ["started", "finished"]
    assert "renewal failed" in caplog.text
