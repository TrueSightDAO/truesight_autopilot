"""Bounded registry of per-key turn locks (rate-limit plan PR3).

Both front-ends keep one lock per conversation unit so two turns for the SAME
topic/channel serialize while different topics stay parallel:

  * ``app/telegram_adapter._thread_dispatch_lock`` -- one lock per (chat, thread)
  * ``app/discord_adapter._channel_dispatch_lock`` -- one lock per channel

Both were plain ``dict[str, threading.Lock]`` that only ever grew: a new key for
every topic the bot ever sees, never evicted. Over months that is a slow leak,
and each leaked entry holds a lock that can never be reclaimed. Discord has the
identical defect (it was copied from Telegram); the plan only mentioned the
Telegram one.

This module gives both a shared, bounded registry instead of two hand-rolled
copies, so there is exactly one place to reason about the concurrency.

Why a proxy lock (``TrackedLock``) and not a raw ``threading.Lock``:
``threading.Lock`` is a factory returning ``_thread.lock``, which cannot be
subclassed, so the only way to observe acquire/release is to wrap it. The
registry needs that observation to answer "is anyone holding this?" -- without
it, eviction can only guess from timestamps.

Eviction soundness (the interesting part)
-----------------------------------------
Evicting a lock another turn is *using* would let a second turn create a fresh
lock for the same key and run concurrently with the first -- exactly the race
the lock exists to prevent. So an entry is only ever evicted when
``depth == 0`` (nobody holds it), and the eviction test happens under the same
``_guard`` that ``acquire()`` uses to publish ``depth``, so a holder cannot slip
in between the test and the delete.

There is one residual window we accept and bound rather than claim away: a
thread may call ``get(key)`` (returning the proxy) and only acquire it some time
later. Between those two moments ``depth`` is still 0, so it looks idle. We make
that window harmless by evicting on **idleness timeout** first -- an entry is
only a candidate after ``ttl`` seconds (default 30 min) with no use -- so the
thread would have to sit between ``get`` and ``acquire`` for half an hour. The
size-based cap is a last resort, and at cap 500 with a 10-worker pool it is very
unlikely to be reached at all. Worst case it costs one duplicated turn, never
corrupted state; the alternative (unbounded growth) is strictly worse.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

__all__ = ["LockRegistry", "TrackedLock"]

# Cap on live entries. Sized so the working set (distinct topics active per TTL)
# stays well under it in normal operation; 500 >> the 10 handler workers.
DEFAULT_MAX_LOCKS = int(os.getenv("TURN_LOCK_MAX", "500"))
# Idle time after which an unused lock may be reclaimed.
DEFAULT_TTL_SECONDS = float(os.getenv("TURN_LOCK_TTL", "1800"))


class TrackedLock:
    """A ``threading.Lock`` plus a registry-observable holder count.

    Supports the subset of the lock protocol the adapters use: ``acquire``,
    ``release``, ``locked``, ``with``. ``depth`` is a *count*, not a bool, so a
    re-entrant caller that takes the same lock twice keeps it pinned at 2 and it
    will not be reclaimed while genuinely held.
    """

    __slots__ = ("_lock", "_registry", "_key")

    def __init__(self, registry: "LockRegistry", key: str) -> None:
        self._lock = threading.Lock()
        self._registry = registry
        self._key = key

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._registry._note_acquired(self._key)
        return ok

    def release(self) -> None:
        # Order matters: drop the count BEFORE freeing the lock, so a reaper can
        # never see depth>0 on a lock that is already free (which would just
        # delay eviction -- safe) or depth==0 on one still held (unsafe).
        self._registry._note_released(self._key)
        self._lock.release()

    def __enter__(self) -> "TrackedLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TrackedLock key={self._key!r} locked={self.locked()}>"


class LockRegistry:
    """Dict-like registry of ``TrackedLock`` keyed by an opaque string.

    Deliberately exposes ``clear()`` and ``__len__`` so existing tests that reset
    module-level state with ``..._dispatch_locks.clear()`` keep working.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_LOCKS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        *,
        clock=time.monotonic,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._guard = threading.Lock()
        self._entries: dict[str, TrackedLock] = {}
        self._depth: dict[str, int] = {}
        self._last_used: dict[str, float] = {}

    # ── public ────────────────────────────────────────────────────────────────
    def get(self, key: str) -> TrackedLock:
        """Return the stable lock for ``key``, creating and evicting as needed."""
        with self._guard:
            lock = self._entries.get(key)
            if lock is None:
                lock = TrackedLock(self, key)
                self._entries[key] = lock
                self._depth.setdefault(key, 0)
            self._last_used[key] = self._clock()
            if len(self._entries) > self._max_entries:
                self._reclaim_locked()
            return lock

    def clear(self) -> None:
        """Drop every entry (tests / process restart). Held locks stay valid."""
        with self._guard:
            self._entries.clear()
            self._depth.clear()
            self._last_used.clear()

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        with self._guard:
            return {
                "entries": len(self._entries),
                "held": sum(1 for d in self._depth.values() if d > 0),
                "max_entries": self._max_entries,
            }

    # ── registry-internal (called by TrackedLock, must take _guard) ───────────
    def _note_acquired(self, key: str) -> None:
        with self._guard:
            if key in self._entries:
                self._depth[key] = self._depth.get(key, 0) + 1
                self._last_used[key] = self._clock()

    def _note_released(self, key: str) -> None:
        with self._guard:
            if key in self._entries:
                self._depth[key] = max(0, self._depth.get(key, 0) - 1)
                self._last_used[key] = self._clock()

    # ── eviction (caller already holds _guard) ────────────────────────────────
    def _reclaim_locked(self) -> None:
        """Shrink back to the cap. Phase 1: idle-timeout. Phase 2: oldest idle."""
        now = self._clock()
        surplus = len(self._entries) - self._max_entries
        if surplus <= 0:
            return

        # Phase 1 -- entries nobody holds and unused beyond the TTL. Oldest first.
        expired = sorted(
            (t, k)
            for k, t in self._last_used.items()
            if self._depth.get(k, 0) == 0 and (now - t) >= self._ttl
        )
        for _t, key in expired[:surplus]:
            self._evict(key)
            surplus -= 1
            if surplus <= 0:
                return

        # Phase 2 -- still over cap (burst of distinct keys): take the oldest
        # entries nobody holds, probing with a non-blocking acquire so "idle" is
        # proven rather than inferred.
        idle = sorted(
            (t, k) for k, t in self._last_used.items() if self._depth.get(k, 0) == 0
        )
        for _t, key in idle:
            if surplus <= 0:
                return
            lock = self._entries.get(key)
            if lock is None:
                continue
            if lock._lock.acquire(blocking=False):
                lock._lock.release()
                self._evict(key)
                surplus -= 1

    def _evict(self, key: str) -> None:
        self._entries.pop(key, None)
        self._depth.pop(key, None)
        self._last_used.pop(key, None)
