"""Tests for the bounded turn-lock registry (rate-limit plan PR3).

The registry replaces two unbounded ``dict[str, threading.Lock]`` maps (one in
each front-end) whose entries were never reclaimed. The properties that matter:

1. stability  -- the same key always yields the same lock object
2. bounded    -- the map cannot grow without limit
3. sound      -- a lock someone is HOLDING is never evicted (or two turns for
                 the same topic run concurrently, the exact race it prevents)
4. re-entrant -- a key taken twice keeps its holder count at 2, so it is never
                 mistaken for idle
5. reuse      -- keys freed by an earlier turn are reused, not leaked forever
"""

from __future__ import annotations

import threading

import pytest

from app.lock_registry import LockRegistry, TrackedLock


class FakeClock:
    """Controllable monotonic clock so TTL tests need no sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


# ── 1. stability ──────────────────────────────────────────────────────────────
def test_same_key_same_lock_and_distinct_keys_distinct_locks():
    reg = LockRegistry(max_entries=10, ttl_seconds=60)
    a1 = reg.get("a")
    a2 = reg.get("a")
    b = reg.get("b")
    assert a1 is a2
    assert a1 is not b
    assert isinstance(a1, TrackedLock)


def test_lock_works_as_context_manager_and_reports_locked():
    reg = LockRegistry(max_entries=10, ttl_seconds=60)
    lock = reg.get("k")
    assert lock.locked() is False
    with lock:
        assert lock.locked() is True
    assert lock.locked() is False


def test_clear_empties_registry_but_old_locks_stay_usable():
    """Tests reset shared state with .clear(); held/old references must not break."""
    reg = LockRegistry(max_entries=10, ttl_seconds=60)
    old = reg.get("k")
    assert len(reg) == 1
    reg.clear()
    assert len(reg) == 0
    # The stale reference still behaves like a lock (no KeyError on acquire).
    with old:
        assert old.locked() is True


# ── 2. bounded ────────────────────────────────────────────────────────────────
def test_registry_never_exceeds_cap():
    reg = LockRegistry(max_entries=50, ttl_seconds=0)
    for i in range(500):
        reg.get(f"topic-{i}")
        assert len(reg) <= 50
    assert len(reg) <= 50


def test_expired_idle_entries_are_reclaimed():
    clock = FakeClock()
    reg = LockRegistry(max_entries=10, ttl_seconds=60, clock=clock)
    for i in range(10):
        reg.get(f"old-{i}")
    assert len(reg) == 10
    clock.advance(3600)  # everything is now idle past TTL
    for i in range(10):
        reg.get(f"new-{i}")
    # 10 expired were reclaimed, so the 10 new keys all fit.
    assert len(reg) == 10
    assert len([k for k in ("new-0", "new-9") if reg.get(k) is not None]) == 2


# ── 3. sound: never evict a held lock ─────────────────────────────────────────
def test_held_lock_is_not_evicted_even_when_registry_is_full():
    """The correctness core: evicting a held lock would let a second turn for the
    same key create a fresh lock and run concurrently with the first."""
    clock = FakeClock()
    reg = LockRegistry(max_entries=4, ttl_seconds=60, clock=clock)
    held_key = "busy-topic"
    lock = reg.get(held_key)
    lock.acquire()
    try:
        clock.advance(3600)  # held lock is ancient -- would look idle by timestamp
        for i in range(50):
            reg.get(f"other-{i}")
        # Still the same object, still held, still in the registry.
        assert reg.get(held_key) is lock
        assert lock.locked() is True
        assert reg.stats()["held"] >= 1
    finally:
        lock.release()


def test_no_duplicate_lock_is_created_for_a_held_key_under_pressure():
    """Re-get the same busy key many times while it is held; identity must hold."""
    reg = LockRegistry(max_entries=3, ttl_seconds=0)
    lock = reg.get("k")
    lock.acquire()
    try:
        seen = {id(reg.get("k")) for _ in range(100)}
        assert seen == {id(lock)}
        # And bumping other traffic through a tiny cap didn't clone it.
        for i in range(100):
            reg.get(f"x{i}")
        assert reg.get("k") is lock
    finally:
        lock.release()


# ── 4. re-entrant holder counting ────────────────────────────────────────────
def test_double_acquire_keeps_holder_count_at_two():
    reg = LockRegistry(max_entries=5, ttl_seconds=0)
    lock = reg.get("k")
    lock.acquire()
    lock.release()  # depth 1 -> 0 but underlying lock is free
    assert reg.stats()["held"] == 0
    # Now genuinely nested (only possible if the same thread re-locks -- not a
    # supported pattern for threading.Lock, so we exercise the counting API).
    lock.acquire()
    reg._note_acquired("k")  # simulate a second logical holder
    assert reg._depth["k"] == 2
    reg._note_released("k")
    assert reg._depth["k"] == 1
    lock.release()
    assert reg.stats()["held"] == 0


def test_release_clamps_at_zero():
    reg = LockRegistry(max_entries=5, ttl_seconds=60)
    reg.get("k")
    reg._note_released("k")  # never acquired
    assert reg._depth["k"] == 0


# ── 5. concurrency smoke ─────────────────────────────────────────────────────
def test_concurrent_get_returns_one_lock_per_key():
    reg = LockRegistry(max_entries=1000, ttl_seconds=60)
    results: list[TrackedLock] = []
    lock = threading.Lock()

    def worker():
        got = reg.get("shared")
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(r) for r in results}) == 1


def test_concurrent_turns_on_same_key_serialize():
    """End-to-end: two threads holding the same key never overlap."""
    reg = LockRegistry(max_entries=100, ttl_seconds=60)
    order: list[str] = []
    gate = threading.Lock()

    def turn(name: str):
        with reg.get("same-topic"):
            with gate:
                order.append(f"{name}-in")
            # Tiny critical section; overlap would show as interleaving.
            for _ in range(1000):
                pass
            with gate:
                order.append(f"{name}-out")

    ts = [threading.Thread(target=turn, args=(n,)) for n in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # Each entrant must have exited before the other entered.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


# ── 6. adapter wiring (both front-ends share the registry) ───────────────────
def test_telegram_adapter_uses_bounded_registry():
    from app import telegram_adapter as ta

    ta._thread_dispatch_locks.clear()
    assert ta._thread_dispatch_lock(-100, 5) is ta._thread_dispatch_lock(-100, 5)
    assert ta._thread_dispatch_lock(-100, 5) is not ta._thread_dispatch_lock(-100, 6)
    # None and 0 collapse onto the same bare-topic key (unchanged contract).
    assert ta._thread_dispatch_lock(-100, None) is ta._thread_dispatch_lock(-100, 0)
    assert isinstance(ta._thread_dispatch_lock(-100, 7), TrackedLock)


def test_discord_adapter_uses_bounded_registry():
    from app import discord_adapter as da

    da._channel_dispatch_locks.clear()
    a1 = da._channel_dispatch_lock("c1")
    assert a1 is da._channel_dispatch_lock("c1")
    assert a1 is not da._channel_dispatch_lock("c2")
    # int and str channel ids normalise to one key (unchanged contract).
    assert da._channel_dispatch_lock(555) is da._channel_dispatch_lock("555")
    assert isinstance(a1, TrackedLock)


def test_oracle_rate_limit_is_guarded():
    """PR3 audit: the limiter's get->compare->set is now under a lock."""
    from app import main

    assert hasattr(main, "_oracle_rate_limit_guard")
    main._oracle_rate_limit.clear()
    main._check_oracle_rate_limit("1.2.3.4")
    with pytest.raises(Exception) as exc:
        main._check_oracle_rate_limit("1.2.3.4")
    assert getattr(exc.value, "status_code", None) == 429
    # A different IP is unaffected.
    main._check_oracle_rate_limit("5.6.7.8")
    main._oracle_rate_limit.clear()
