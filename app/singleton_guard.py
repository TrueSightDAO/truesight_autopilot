"""Cross-process singleton guard for ``lifespan()``'s background loops.

Why this exists
---------------
``uvicorn ... --workers N`` runs ``lifespan()`` once *per worker*, so every
``asyncio.create_task(loop())`` site in ``app/main.py`` starts N independent
copies of the same loop. Request handling is fine with that (per-session state
lives in Redis), but nine of those tasks are *housekeeping singletons* --
cron-ish loops that poll an external system and post a digest. With
``--workers 4`` each one fired 4x: all four workers' calendar watchdogs fired
within 4 seconds of each other and each posted its own duplicate
Telegram/Discord digest (2026-10-29).

What this does
--------------
One thin layer over the already-shipped ``app/redis_claim.py`` primitives -- no
new Redis code: a worker must win ``SET NX EX`` on ``lock:singleton:<name>``
before it may run the loop. Exactly one worker in the fleet wins; the losers
log one line and return. The winner renews every ``ttl/3`` so its claim
outlives a long-running loop, while a dead winner's key still expires within
one TTL window.

Degrade-open, deliberately
--------------------------
If Redis is unreachable, ``run_as_singleton`` logs and runs the loop anyway.
That keeps the single-worker default working (nothing races, so claiming is a
no-op there) and makes a Redis blip a return to the old "every worker runs it"
behaviour rather than silently killing all nine background loops. The same
applies mid-flight: a failed renewal logs and keeps the loop running instead of
tearing it down.

Known limitation (not fixed here, on purpose)
---------------------------------------------
A loser returns for good. If the *winner* dies, its key expires within one TTL
window, but no surviving worker re-attempts the claim until that worker's
process restarts (deploy, crash-restart, autoscale). So this guard removes
duplicate work in steady state; it does not implement hot failover. Adding a
bounded re-claim retry loop is a separate change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable
from typing import Any

from . import redis_claim

logger = logging.getLogger(__name__)

#: Seconds a singleton claim lives without renewal.
SINGLETON_TTL_SECS = 120
#: Renew this many times per TTL (3 -> renew every 40s at the default TTL).
RENEW_FRACTION = 3
#: Floor on the renewal interval so tiny test TTLs cannot busy-spin.
MIN_RENEW_INTERVAL_SECS = 0.2


def default_worker_id() -> str:
    """Identify this process within the fleet: host + pid."""
    return f"{socket.gethostname()}:{os.getpid()}"


def singleton_key(name: str) -> str:
    """The ``redis_claim`` thread id used for a named singleton."""
    return f"singleton:{name}"


def _close_quietly(coro: Any) -> None:
    """Close a coroutine we won't await (silences 'never awaited' warnings)."""
    close = getattr(coro, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


async def run_as_singleton(
    name: str,
    coro: Awaitable[None],
    *,
    ttl: int = SINGLETON_TTL_SECS,
    client: Any = None,
    worker_id: str | None = None,
) -> None:
    """Await *coro* only if this worker wins the ``singleton:<name>`` claim.

    Losers log once and return without running the loop, discarding the
    coroutine they were handed. Failures to talk to Redis degrade OPEN (the
    loop runs) so a cache outage can never take the background loops down.
    """
    wid = worker_id or default_worker_id()
    key = singleton_key(name)

    try:
        won = redis_claim.try_claim(key, wid, ttl=ttl, client=client)
    except Exception as exc:
        logger.warning(
            "Singleton guard unavailable for '%s' (%s: %s) -- running it in this "
            "worker anyway (degrade-open; duplicate work is possible while Redis "
            "is unreachable)",
            name,
            type(exc).__name__,
            exc,
        )
        try:
            await coro
        except Exception:
            logger.exception("Singleton loop '%s' crashed (unguarded run)", name)
        return

    if not won:
        logger.info(
            "Singleton task '%s' already owned by another worker -- skipping", name
        )
        _close_quietly(coro)
        return

    logger.info("Singleton task '%s' claimed by %s (ttl=%ss)", name, wid, ttl)

    async def _renew_forever() -> None:
        interval = max(MIN_RENEW_INTERVAL_SECS, ttl / RENEW_FRACTION)
        while True:
            await asyncio.sleep(interval)
            try:
                still_ours = redis_claim.renew(key, wid, ttl=ttl, client=client)
            except Exception as exc:
                logger.warning(
                    "Singleton '%s' renewal failed (%s: %s) -- loop keeps running",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            if not still_ours:
                logger.warning(
                    "Singleton '%s' claim is no longer ours (expired and re-taken) "
                    "-- stopping renewals; the loop is left running",
                    name,
                )
                return

    renew_task = asyncio.create_task(_renew_forever(), name=f"singleton-renew:{name}")
    try:
        await coro
    except Exception:
        logger.exception("Singleton loop '%s' crashed -- releasing its claim", name)
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task
        try:
            released = redis_claim.release(key, wid, client=client)
        except Exception as exc:
            logger.warning(
                "Singleton '%s' release failed (%s: %s) -- claim expires within %ss",
                name,
                type(exc).__name__,
                exc,
                ttl,
            )
        else:
            if released:
                logger.info("Singleton task '%s' released by %s", name, wid)
