"""PR1 -- Redis mark-and-claim primitives, tested against the REAL local Redis.

Plan: SOPHIA_REDIS_MARK_AND_CLAIM_DISPATCH_PLAN.md section 2, PR1.

These are NOT mocked. Every test talks to the live local Redis instance on
127.0.0.1:6379 (plan section 1: "Unit tests against the real local Redis (not a
mock)"). Mocking a distributed-lock primitive would test the mock, not Redis's
actual SET NX / TTL / Lua semantics -- which is precisely what mutual exclusion
depends on.

Environment note: CI runners have no Redis, so the whole module skips there with
a clear reason. Locally, if Redis is unreachable the module skips with the
connection error printed, so a broken instance is never mistaken for a pass.

HONEST LIMITATION (plan section 0): this exercises the mechanism in isolation.
Sophia is a single process today, so it does not and cannot demonstrate
multi-process behaviour -- there is no second worker to coordinate with.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import redis_claim as rc
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"redis_claim import unavailable: {exc}", allow_module_level=True)


def _client_or_skip():
    """Live local Redis client, or skip the module with the real reason."""
    try:
        client = rc.get_client()
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"local Redis not reachable at {rc._redis_url()} ({type(exc).__name__}: "
            f"{str(exc)[:120]}) -- these tests require the real instance, never a mock",
            allow_module_level=True,
        )


CLIENT = _client_or_skip()


@pytest.fixture
def thread_id() -> str:
    """A unique thread id per test, so tests never collide in the shared Redis."""
    tid = f"pytest:{uuid.uuid4().hex[:12]}"
    yield tid
    # Best-effort cleanup: the fixture must not leak keys into the shared DB.
    CLIENT.delete(rc.lock_key(tid))
    CLIENT.zrem(rc.MARKS_KEY, tid)


# --- claim: succeeds when free, fails when held -----------------------------
def test_claim_succeeds_when_free(thread_id):
    assert rc.owner_of(thread_id, CLIENT) is None
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    assert rc.owner_of(thread_id, CLIENT) == "workerA"


def test_claim_fails_when_held_by_another_worker(thread_id):
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    # a second, different worker must lose
    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is False
    # and the original owner is untouched
    assert rc.owner_of(thread_id, CLIENT) == "workerA"


def test_claim_is_not_reentrant_for_same_worker(thread_id):
    """SET NX fails even for the same worker_id -- no silent double-entry."""
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is False


def test_only_one_of_many_concurrent_claims_wins(thread_id):
    """The core mutual-exclusion property, exercised directly."""
    results = [
        rc.try_claim(thread_id, f"worker{i}", ttl=30, client=CLIENT) for i in range(8)
    ]
    assert results.count(True) == 1, results
    winner_idx = results.index(True)
    assert rc.owner_of(thread_id, CLIENT) == f"worker{winner_idx}"


# --- release: only the actual owner ----------------------------------------
def test_release_succeeds_for_actual_owner(thread_id):
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    assert rc.release(thread_id, "workerA", client=CLIENT) is True
    assert rc.owner_of(thread_id, CLIENT) is None
    # and it is genuinely free again
    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is True


def test_release_fails_for_non_owner_and_leaves_lock_intact(thread_id):
    """The check-owner-then-delete guarantee: never a plain DEL."""
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    assert rc.release(thread_id, "workerB", client=CLIENT) is False
    # workerB must NOT have freed workerA's lock
    assert rc.owner_of(thread_id, CLIENT) == "workerA"
    # workerB also cannot sneak in afterwards
    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is False


def test_release_on_unclaimed_thread_is_false(thread_id):
    assert rc.release(thread_id, "workerA", client=CLIENT) is False


def test_stale_owner_cannot_release_a_lock_reclaimed_by_another(thread_id):
    """The exact race the Lua script exists to prevent.

    workerA's TTL lapses, workerB legitimately claims, then workerA wakes up and
    tries to release. A plain DEL would drop workerB's lock; ours must not.
    """
    assert rc.try_claim(thread_id, "workerA", ttl=1, client=CLIENT) is True
    time.sleep(1.2)  # let workerA's claim actually expire
    assert rc.owner_of(thread_id, CLIENT) is None

    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is True
    # late-arriving workerA now tries to clean up "its" lock
    assert rc.release(thread_id, "workerA", client=CLIENT) is False
    assert rc.owner_of(thread_id, CLIENT) == "workerB"


# --- claim becomes available again after TTL expiry -------------------------
def test_claim_available_again_after_ttl_expiry(thread_id):
    assert rc.try_claim(thread_id, "workerA", ttl=1, client=CLIENT) is True
    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is False
    time.sleep(1.2)
    # workerA's dead claim no longer blocks the thread
    assert rc.try_claim(thread_id, "workerB", ttl=30, client=CLIENT) is True
    assert rc.owner_of(thread_id, CLIENT) == "workerB"


def test_claim_ttl_remaining_reflects_ex_setting(thread_id):
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True
    remaining = rc.claim_ttl_remaining(thread_id, CLIENT)
    assert 0 < remaining <= 30
    assert rc.claim_ttl_remaining(thread_id + ":never", CLIENT) == -2


# --- argument validation ----------------------------------------------------
@pytest.mark.parametrize("bad_ttl", [0, -1])
def test_claim_rejects_nonpositive_ttl(thread_id, bad_ttl):
    with pytest.raises(ValueError):
        rc.try_claim(thread_id, "workerA", ttl=bad_ttl, client=CLIENT)


@pytest.mark.parametrize("blank", ["", None])
def test_claim_and_release_reject_blank_worker_id(thread_id, blank):
    with pytest.raises(ValueError):
        rc.try_claim(thread_id, blank, client=CLIENT)
    with pytest.raises(ValueError):
        rc.release(thread_id, blank, client=CLIENT)


# --- mark -------------------------------------------------------------------
def test_mark_records_and_is_idempotent(thread_id):
    """Re-marking must not create duplicates, but must refresh the timestamp."""
    assert rc.is_marked(thread_id, CLIENT) is False
    rc.mark(thread_id, CLIENT, now=1000.0)
    assert rc.is_marked(thread_id, CLIENT) is True
    score_first = CLIENT.zscore(rc.MARKS_KEY, thread_id)

    rc.mark(thread_id, CLIENT, now=2000.0)
    assert CLIENT.zscore(rc.MARKS_KEY, thread_id) == 2000.0
    # exactly one member for this thread -- no duplicate accumulation.
    # Counted by thread, NOT via global zcard: this suite shares a Redis database
    # with the running service, so the zset legitimately holds other live marks.
    assert (
        sum(1 for m in CLIENT.zrange(rc.MARKS_KEY, 0, -1) if m == thread_id) == 1
    )
    assert score_first == 1000.0


def test_mark_is_not_a_claim(thread_id):
    """The invariant this whole plan protects: marking grants no exclusivity."""
    rc.mark(thread_id, CLIENT)
    assert rc.is_marked(thread_id, CLIENT) is True
    # a mark must not create a lock, so any number of workers can still see it
    # as free and race for the claim
    assert rc.owner_of(thread_id, CLIENT) is None
    assert rc.try_claim(thread_id, "workerA", ttl=30, client=CLIENT) is True


def test_clear_mark(thread_id):
    rc.mark(thread_id, CLIENT)
    assert rc.clear_mark(thread_id, CLIENT) is True
    assert rc.is_marked(thread_id, CLIENT) is False
    assert rc.clear_mark(thread_id, CLIENT) is False


# --- key hygiene ------------------------------------------------------------
def test_lock_keys_are_namespaced():
    """Locks must be per-thread namespaced, or two threads would collide."""
    assert rc.lock_key("abc") == "lock:abc"
    assert rc.lock_key(42486) == "lock:42486"
    assert rc.lock_key("abc") != rc.lock_key("abd")
