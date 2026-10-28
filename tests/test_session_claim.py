"""PR1 -- layer-2 cross-process session claim (``app/session_claim.py``).

These pin the contract that makes the Redis claim safe to *layer* over the
always-on ``asyncio.Lock`` instead of replacing it:

* default-OFF must not touch Redis at all (no client is ever constructed);
* a free session is claimed, and released, on the expected key;
* a peer's claim makes us wait a BOUNDED time, log the real holder, then degrade
  to layer-1-only rather than blocking the request forever;
* a stale holder cannot delete a newer holder's claim (the cross-process mirror
  of the pinned layer-1 test);
* renewal extends the TTL of a claim we still own and refuses one we do not;
* a Redis failure degrades instead of breaking the turn.

``FakeRedis`` emulates exactly the three commands ``redis_claim`` issues (SET NX
EX / GET / EVAL), so no live Redis and no new test dependency are needed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import redis_claim, session_claim
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app imports unavailable: {exc}", allow_module_level=True)

_MAIN_IMPORT_ERROR: Exception | None = None
try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    m = None  # type: ignore[assignment]
    _MAIN_IMPORT_ERROR = exc

SID = "tg:-100999:42486"


class FakeRedis:
    """Minimal in-memory stand-in for the Redis commands ``redis_claim`` uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = False
        self.calls: list[str] = []

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        self.calls.append("set")
        if self.down:
            raise ConnectionError("redis unreachable")
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key: str):
        if self.down:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    def eval(self, script: str, numkeys: int, key: str, *args):
        self.calls.append("eval")
        if self.down:
            raise ConnectionError("redis unreachable")
        owner = args[0]
        if script is redis_claim._RELEASE_LUA:
            if self.store.get(key) == owner:
                self.store.pop(key, None)
                self.ttls.pop(key, None)
                return 1
            return 0
        if script is redis_claim._RENEW_LUA:
            if self.store.get(key) == owner:
                self.ttls[key] = int(args[1])
                return 1
            return 0
        raise AssertionError("unexpected Lua script")


@pytest.fixture(autouse=True)
def _clean_state():
    """No renewer, no cached pid, no layer-1 state leaks between tests."""
    session_claim.stop_renewer(SID)
    session_claim.reset_process_id_cache()
    if m is not None:
        m._session_locks.pop(SID, None)
        m._session_lock_holders.pop(SID, None)
        m._session_lock_acquired_at.pop(SID, None)
    yield
    session_claim.stop_renewer(SID)
    if m is not None:
        m._session_locks.pop(SID, None)
        m._session_lock_holders.pop(SID, None)
        m._session_lock_acquired_at.pop(SID, None)


@pytest.fixture
def fake(monkeypatch):
    """Layer 2 switched ON, wired to FakeRedis through ``get_client``."""
    cli = FakeRedis()
    monkeypatch.setattr(redis_claim, "get_client", lambda: cli)
    monkeypatch.setenv("SESSION_CLAIM_ENABLED", "1")
    return cli


def test_disabled_never_touches_redis(monkeypatch):
    """Default-OFF is a hard short-circuit: no client, no call, no error."""

    class Boom:
        def __getattr__(self, name):  # noqa: D105
            raise AssertionError(f"Redis touched while disabled: {name}")

    monkeypatch.delenv("SESSION_CLAIM_ENABLED", raising=False)
    monkeypatch.setattr(redis_claim, "get_client", lambda: Boom())

    assert session_claim.enabled() is False
    assert asyncio.run(session_claim.acquire(SID, "tok")) is None
    assert session_claim.release(None) is False


def test_free_session_is_claimed_then_released(fake):
    claim = asyncio.run(session_claim.acquire(SID, "tok-1"))

    assert claim is not None
    key = session_claim.claim_key(SID)
    assert key == f"lock:session:{SID}"
    assert fake.store[key] == claim.worker_id
    assert claim.worker_id.endswith("|tok-1")  # the holder token rides along
    assert fake.ttls[key] == claim.ttl == session_claim.DEFAULT_TTL_SECS

    assert session_claim.release(claim) is True
    assert key not in fake.store


def test_peer_claim_makes_us_wait_then_degrade(fake, caplog):
    """A held session is waited on for a BOUNDED time, then logged and skipped."""
    key = session_claim.claim_key(SID)
    peer = session_claim.worker_id("peer-token")
    fake.store[key] = peer

    with caplog.at_level("WARNING"):
        claim = asyncio.run(session_claim.acquire(SID, "tok-2", wait=0.3))

    assert claim is None
    text = caplog.text
    assert "SESSION CLAIM" in text
    assert "peer-token" in text  # names the real holder, not "<unknown>"
    assert "DEGRADED" in text
    assert fake.store[key] == peer  # we never clobbered the peer's claim


def test_stale_holder_cannot_release_a_newer_claim(fake):
    """The layer-1 token-mismatch guarantee must survive the process boundary."""
    key = session_claim.claim_key(SID)
    stale = asyncio.run(session_claim.acquire(SID, "old"))
    assert stale is not None

    # our TTL lapses and a newer turn wins the session
    fake.store.pop(key)
    newer = asyncio.run(session_claim.acquire(SID, "new"))
    assert newer is not None and newer.worker_id != stale.worker_id

    # the stale holder wakes up and releases: must NOT free the newer claim
    assert session_claim.release(stale) is False
    assert fake.store[key] == newer.worker_id


def test_renew_extends_ttl_only_for_the_owner(fake):
    claim = asyncio.run(session_claim.acquire(SID, "tok"))
    assert claim is not None
    key = session_claim.claim_key(SID)
    ns = f"{session_claim.CLAIM_KEY_PREFIX}{SID}"
    fake.ttls[key] = 10

    assert redis_claim.renew(ns, claim.worker_id, 99) is True
    assert fake.ttls[key] == 99

    # a non-owner cannot extend it, and the owner's TTL is left alone
    assert redis_claim.renew(ns, "someone-else", 5) is False
    assert fake.ttls[key] == 99


@pytest.mark.parametrize("bad_ttl", [0, -1])
def test_renew_rejects_a_nonpositive_ttl(fake, bad_ttl):
    with pytest.raises(ValueError):
        redis_claim.renew(f"{session_claim.CLAIM_KEY_PREFIX}{SID}", "w", bad_ttl)


def test_redis_failure_degrades_instead_of_breaking_the_turn(fake, caplog):
    fake.down = True
    with caplog.at_level("ERROR"):
        claim = asyncio.run(session_claim.acquire(SID, "tok"))

    assert claim is None
    assert "SESSION CLAIM" in caplog.text
    assert "KL-1" in caplog.text  # names the combined-failure limitation


def test_guard_claims_and_releases_around_a_turn(fake):
    """Integration: ``_session_lock_guard`` takes layer 2 and always gives it back."""
    if m is None:
        pytest.skip(f"app.main unavailable: {_MAIN_IMPORT_ERROR}")

    key = session_claim.claim_key(SID)
    seen: list[str | None] = []

    async def turn() -> None:
        async with m._session_lock_guard(SID):
            seen.append(fake.store.get(key))
            assert session_claim.renewer_running(SID) is True

    asyncio.run(turn())

    assert seen and seen[0] is not None and "|" in seen[0]
    assert key not in fake.store  # released
    assert session_claim.renewer_running(SID) is False  # renewer stopped
    assert not m._session_lock(SID).locked()  # layer 1 also released
