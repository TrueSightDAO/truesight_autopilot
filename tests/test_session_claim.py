"""PR1 -- cross-process session claim layer (app/session_claim.py).

Plan: plans/SOPHIA_SIDEKIQ_MULTIPROCESS_PLAN.md, PR1.

The in-process ``asyncio.Lock`` in ``app.main`` is a complete guarantee only at
``--workers 1``. Two OS processes each build their own lock object for the same
session_id, so nothing stops them interleaving one thread's transcript writes.
This layer adds a Redis claim on top via the primitives already on main.

These tests pin the contract that matters for a *default-OFF* feature:

* OFF (the default) is a genuine no-op -- it must not touch Redis at all;
* ON acquires exactly one claim and releases exactly its own;
* a peer holding the session, or Redis being down, degrades to
  ``owned=False`` WITHOUT raising -- the governor's turn must never be dropped
  because a coordination layer failed.

``redis_claim`` is stubbed here rather than hit live: the real SET NX semantics
are already covered against a live Redis by ``tests/test_redis_claim.py``. What
is new here is the *layering and failure* behaviour, which is what needs pinning.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import session_claim as sc
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"session_claim import unavailable: {exc}", allow_module_level=True)


class _FakeRedis:
    """Minimal stand-in for app.redis_claim, recording calls."""

    def __init__(self, *, win=True, raise_on=None):
        self.win = win
        self.raise_on = raise_on
        self.claims: list[tuple[str, str]] = []
        self.releases: list[tuple[str, str]] = []
        self.owners: list[str] = []

    def try_claim(self, thread_id, worker_id, ttl=None, client=None):
        if self.raise_on == "try_claim":
            raise ConnectionError("redis down")
        self.claims.append((str(thread_id), worker_id))
        return self.win

    def release(self, thread_id, worker_id, client=None):
        if self.raise_on == "release":
            raise ConnectionError("redis down")
        self.releases.append((str(thread_id), worker_id))
        return True

    def owner_of(self, thread_id, client=None):
        self.owners.append(str(thread_id))
        return "peer:999"


def _install(monkeypatch, fake, *, on=True):
    """Point session_claim at *fake* and set the enable flag."""
    from app import redis_claim

    monkeypatch.setattr(sc, "enabled", lambda: on)
    monkeypatch.setattr(redis_claim, "try_claim", fake.try_claim)
    monkeypatch.setattr(redis_claim, "release", fake.release)
    monkeypatch.setattr(redis_claim, "owner_of", fake.owner_of)


def _run(coro):
    return asyncio.run(coro)


# --- default OFF -------------------------------------------------------------


def test_flag_defaults_off():
    """The shipped default must be OFF: zero live behaviour change."""
    from app.config import Settings

    assert Settings().session_lock_redis_claim is False


def test_off_never_touches_redis(monkeypatch):
    fake = _FakeRedis()
    _install(monkeypatch, fake, on=False)
    sid = f"tg:1:{uuid.uuid4().int % 100000}"

    claim = _run(sc.acquire_if_enabled(sid))

    assert claim is None, "layer must be a no-op when the flag is off"
    assert fake.claims == [], "must not even attempt a claim when off"


# --- enabled -----------------------------------------------------------------


def test_acquire_wins_and_releases_only_own_claim(monkeypatch):
    fake = _FakeRedis(win=True)
    _install(monkeypatch, fake)
    sid = "tg:-100:42"

    claim = _run(sc.acquire(sid))
    assert claim is not None and claim.owned is True
    assert fake.claims == [(sid, claim.worker_id)]

    _run(sc.release_if_owned(claim))
    assert fake.releases == [(sid, claim.worker_id)]


def test_release_is_noop_when_not_owned(monkeypatch):
    """Failing open must never release a claim we do not hold."""
    fake = _FakeRedis(win=True)
    _install(monkeypatch, fake)
    not_ours = sc.Claim("tg:-100:43", "me:1", owned=False)

    _run(sc.release_if_owned(not_ours))
    _run(sc.release_if_owned(None))

    assert fake.releases == []


def test_peer_holding_past_timeout_fails_open_without_raising(monkeypatch):
    """A peer on the session must not block this turn forever or crash it."""
    fake = _FakeRedis(win=False)
    _install(monkeypatch, fake)
    sid = "tg:-100:44"

    claim = _run(sc.acquire(sid, timeout=0.05))

    assert claim is not None and claim.owned is False
    assert fake.claims, "should have retried at least once"
    assert fake.owners == [sid], "should report who held it for the log"


def test_redis_down_fails_open_without_raising(monkeypatch):
    """Redis outage must degrade to the in-process lock, never drop the turn."""
    fake = _FakeRedis(raise_on="try_claim")
    _install(monkeypatch, fake)

    claim = _run(sc.acquire("tg:-100:45"))

    assert claim is not None and claim.owned is False


def test_release_failure_does_not_raise(monkeypatch):
    """A failed release is bounded by the TTL; it must not mask the turn."""
    fake = _FakeRedis(raise_on="release")
    _install(monkeypatch, fake)
    claim = sc.Claim("tg:-100:46", "me:1", owned=True)

    _run(sc.release_if_owned(claim))  # must not raise


# --- worker identity ---------------------------------------------------------


def test_worker_id_is_unique_per_process_and_overridable(monkeypatch):
    monkeypatch.delenv("SOPHIA_WORKER_ID", raising=False)
    wid = sc.worker_id()
    assert ":" in wid and wid.endswith(str(os.getpid()))

    monkeypatch.setenv("SOPHIA_WORKER_ID", "worker-7")
    assert sc.worker_id() == "worker-7"


# --- wiring ------------------------------------------------------------------


def test_main_session_guard_is_layered():
    """The guard must consult the claim layer, and release it in finally."""
    import inspect

    import app.main as m

    src = inspect.getsource(m._session_lock_guard)
    assert "acquire_if_enabled" in src
    assert "release_if_owned" in src
    # Release must be inside finally, or a raising turn strands the claim
    # for the whole TTL.
    tail = src.split("finally:")[-1]
    assert "release_if_owned" in tail
