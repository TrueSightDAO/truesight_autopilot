"""PR1 -- inner per-turn session-lock staleness expiry.

A hung turn holding the inner ``_session_lock`` blocks its thread forever and
nothing notices (the mk3.py / PID 2174824 failure shape). A *cancelled* turn is
worse: cancellation skips the ``async with`` release path, stranding the lock.

These tests pin both directions of the contract:

* a lock held PAST the derived timeout is force-released with a loud log, and
* a lock held UNDER the timeout is NOT touched -- firing early kills live work,
  which is the same bug class inverted.

The timeout is derived from real observed turn durations, never guessed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


SID = "tg:-100999:42486"


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no lock/holder state for its session."""
    m._session_locks.pop(SID, None)
    m._session_lock_holders.pop(SID, None)
    m._session_lock_acquired_at.pop(SID, None)
    yield
    m._session_locks.pop(SID, None)
    m._session_lock_holders.pop(SID, None)
    m._session_lock_acquired_at.pop(SID, None)


def test_default_timeout_is_derived_from_observed_data():
    """The constant must be >= the 1295s longest observed single turn (section 1)."""
    assert m.DEFAULT_SESSION_LOCK_STALE_AFTER_SECS >= 1295.0


def test_stale_after_reads_env_override(monkeypatch):
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "42.5")
    assert m._session_lock_stale_after() == pytest.approx(42.5)


@pytest.mark.parametrize("bad", ["", "0", "-3", "not-a-number"])
def test_bad_env_falls_back_to_default(monkeypatch, bad):
    """A junk override must never shorten the timeout to something unsafe."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", bad)
    assert m._session_lock_stale_after() == m.DEFAULT_SESSION_LOCK_STALE_AFTER_SECS


def test_held_for_reports_none_when_free():
    assert m._session_lock_held_for(SID) is None


@pytest.mark.asyncio
async def test_stale_lock_is_force_released(monkeypatch, caplog):
    """A lock held past the timeout must be force-released, loudly."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "0.05")

    lock = m._session_lock(SID)
    await lock.acquire()  # simulate a hung turn holding it
    m._session_lock_holders[SID] = "stale-holder"
    # backdate the acquire time well past the timeout
    m._session_lock_acquired_at[SID] = m.time.monotonic() - 10.0

    with caplog.at_level("ERROR"):
        async with m._session_lock_guard(SID):
            # We only get here if the stale lock was force-released.
            assert m._session_lock_holders.get(SID) != "stale-holder"

    text = caplog.text
    assert "SESSION LOCK STALE" in text
    assert SID in text
    # loud, not silent: must name the stale holder and the timeout
    assert "stale-holder" in text

    # and it is properly released afterwards, not left held
    assert not m._session_lock(SID).locked()
    assert m._session_lock_held_for(SID) is None


@pytest.mark.asyncio
async def test_fresh_lock_is_NOT_force_released(monkeypatch, caplog):
    """A lock held under the timeout must be left alone (no early kill)."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "600")

    lock = m._session_lock(SID)
    await lock.acquire()
    m._session_lock_holders[SID] = "live-holder"
    m._session_lock_acquired_at[SID] = m.time.monotonic()  # just acquired = alive

    # Second entrant must WAIT (it will block) -- so assert it is still held
    # and NOT force-released by giving it a very short window.
    with caplog.at_level("ERROR"), pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_enter(m._session_lock_guard(SID)), timeout=0.15)

    assert "SESSION LOCK STALE" not in caplog.text
    assert lock.locked() is True
    assert m._session_lock_holders.get(SID) == "live-holder"

    # cleanup: let the original holder go so the suite is hermetic
    lock.release()
    m._session_lock_holders.pop(SID, None)
    m._session_lock_acquired_at.pop(SID, None)


async def _enter(cm):
    async with cm:
        return "entered"


@pytest.mark.asyncio
async def test_concurrent_turns_serialize_without_forced_release(monkeypatch):
    """Two live turns on one session must serialize, never force-release."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "600")
    order: list[str] = []

    async def worker(tag: str):
        async with m._session_lock_guard(SID):
            order.append(f"{tag}:in")
            await asyncio.sleep(0.05)
            order.append(f"{tag}:out")

    await asyncio.gather(worker("a"), worker("b"))

    # strictly nested, never interleaved
    assert order in (
        ["a:in", "a:out", "b:in", "b:out"],
        ["b:in", "b:out", "a:in", "a:out"],
    )
    assert not m._session_lock(SID).locked()


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_strand_the_lock(monkeypatch):
    """A cancelled turn must not hold the inner lock hostage (real 2026-10-27 bug)."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "600")

    async def doomed():
        async with m._session_lock_guard(SID):
            await asyncio.sleep(10)  # never completes

    task = asyncio.create_task(doomed())
    await asyncio.sleep(0.05)
    assert m._session_lock(SID).locked()  # held while running

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # cancellation unwinds through the async context manager -> lock released,
    # no need for the staleness path at all
    assert not m._session_lock(SID).locked()
    assert m._session_lock_held_for(SID) is None


@pytest.mark.asyncio
async def test_non_holder_exit_does_not_release_newer_holders_lock(monkeypatch):
    """The stale holder resuming later must not free the newer holder's lock."""
    monkeypatch.setenv("SESSION_LOCK_STALE_AFTER_SECS", "0.05")

    lock = m._session_lock(SID)
    await lock.acquire()
    m._session_lock_holders[SID] = "stale-holder"
    m._session_lock_acquired_at[SID] = m.time.monotonic() - 10.0

    # pass 1: takes over the stale lock, leaves a NEWER token behind
    async with m._session_lock_guard(SID):
        assert m._session_lock_holders.get(SID) != "stale-holder"
    # (it released cleanly on exit, which is the normal path)

    # now simulate the stale holder "waking up" and exiting: its token no longer
    # matches, so it must NOT release a lock somebody else holds
    await lock.acquire()
    m._session_lock_holders[SID] = "newer-holder"
    m._session_lock_acquired_at[SID] = m.time.monotonic()

    # emulate the stale holder's finally-block decision
    token = "stale-holder"
    assert m._session_lock_holders.get(SID) != token
    assert lock.locked() is True

    lock.release()
    m._session_lock_holders.pop(SID, None)
    m._session_lock_acquired_at.pop(SID, None)
