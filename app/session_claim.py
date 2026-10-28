"""Cross-process session claim layered on top of the in-process asyncio lock.

PR1 of ``plans/SOPHIA_SIDEKIQ_MULTIPROCESS_PLAN.md``.

WHY THIS EXISTS
---------------
The per-session guard in ``app.main._session_lock_guard`` is an in-process
``asyncio.Lock`` (``_session_locks``). With ``--workers 1`` that is a complete
guarantee. With two OS processes each process builds its OWN lock object for the
same ``session_id``, so mutual exclusion silently degrades to nothing and two
workers can interleave transcript writes for one thread -- the race that bricked
threads 3 and 780.

This module adds the missing cross-process layer using the Redis claim
primitives already on main (``app.redis_claim``: ``SET lock:<id> <worker> NX EX``).

DEFAULT OFF
-----------
Gated behind ``settings.session_lock_redis_claim`` (env
``SESSION_LOCK_REDIS_CLAIM``), default **False**. While off this module is never
entered and no live behaviour changes. The worker-count flip that makes it
necessary is a separate human gate (PR5/PR6 of the plan).

KNOWN LIMITATION -- stated, not hidden
--------------------------------------
If Redis is unreachable AND ``workers > 1``, the claim cannot be taken, so this
layer falls OPEN (the turn proceeds) and the cross-process race is NOT closed.
That is acceptable only because ``workers`` stays at 1 until PR5; it must be
re-examined at the PR4 load test before any worker count above 1 is enabled.
Falling open is deliberate: dropping a governor's turn is worse than taking a
turn with a logged warning.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass

from .config import settings

logger = logging.getLogger(__name__)

# How long to wait for a peer worker to release a session before proceeding
# without the cross-process guarantee. Short: the in-process lock still
# serialises this worker, and a long block would stall a governor's turn.
DEFAULT_CLAIM_TIMEOUT_SECS = 30.0

_POLL_INTERVAL_SECS = 0.25


def enabled() -> bool:
    """Whether the cross-process claim layer is switched on (default OFF)."""
    return bool(getattr(settings, "session_lock_redis_claim", False))


def worker_id() -> str:
    """Stable identity for this OS process.

    ``hostname:pid`` is unique per process on one host, so two uvicorn workers
    never collide. An explicit ``SOPHIA_WORKER_ID`` wins, which is what a
    multi-HOST deployment would need (pid alone would not disambiguate).
    """
    explicit = os.environ.get("SOPHIA_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class Claim:
    """Outcome of an acquisition attempt.

    ``owned`` True means this process genuinely holds the cross-process claim
    and must release it. ``owned`` False means the turn is proceeding on the
    in-process lock alone (Redis down, or a peer held it past the timeout).
    """

    session_id: str
    worker_id: str
    owned: bool


async def acquire_if_enabled(
    session_id: str, timeout: float | None = None
) -> Claim | None:
    """Acquire only when the flag is on; ``None`` means "layer not in use"."""
    if not enabled():
        return None
    return await acquire(session_id, timeout=timeout)


async def acquire(session_id: str, timeout: float | None = None) -> Claim:
    """Try to claim *session_id*, retrying until *timeout*, then fail open.

    Never raises: a Redis outage or a peer holding the session degrades to
    ``owned=False`` with a loud log, because the caller has already taken the
    in-process lock and must not lose the governor's turn.
    """
    from . import redis_claim as rc

    wid = worker_id()
    budget = DEFAULT_CLAIM_TIMEOUT_SECS if timeout is None else timeout
    deadline = time.monotonic() + budget

    while True:
        try:
            # to_thread: redis-py is blocking, and blocking the event loop here
            # would stall every other in-flight turn on this worker.
            won = await asyncio.to_thread(rc.try_claim, session_id, wid)
        except Exception as exc:  # noqa: BLE001 -- fail open, never lose the turn
            logger.error(
                "SESSION CLAIM: Redis unavailable for session=%s (%s) -- proceeding "
                "WITHOUT a cross-process claim. Safe only while workers==1; do not "
                "run workers>1 with Redis down.",
                session_id,
                exc,
            )
            return Claim(session_id, wid, False)

        if won:
            logger.info("SESSION CLAIM: acquired session=%s worker=%s", session_id, wid)
            return Claim(session_id, wid, True)

        if time.monotonic() >= deadline:
            owner = None
            try:
                owner = await asyncio.to_thread(rc.owner_of, session_id)
            except Exception as exc:  # noqa: BLE001 -- diagnostics only
                logger.debug(
                    "SESSION CLAIM: could not read owner of session=%s (%s)",
                    session_id,
                    exc,
                )
            logger.error(
                "SESSION CLAIM: session=%s still held by worker=%s after %.1fs -- "
                "proceeding on the in-process lock only. Cross-process exclusion "
                "is NOT guaranteed for this turn.",
                session_id,
                owner or "<unknown>",
                budget,
            )
            return Claim(session_id, wid, False)

        await asyncio.sleep(_POLL_INTERVAL_SECS)


async def release_if_owned(claim: Claim | None) -> None:
    """Release the claim, but only if this process actually owns it."""
    if claim is None or not claim.owned:
        return

    from . import redis_claim as rc

    try:
        released = await asyncio.to_thread(
            rc.release, claim.session_id, claim.worker_id
        )
        if not released:
            logger.error(
                "SESSION CLAIM: release of session=%s found a different owner -- "
                "left that worker's claim intact",
                claim.session_id,
            )
    except Exception as exc:  # noqa: BLE001 -- TTL bounds a leaked claim
        logger.error(
            "SESSION CLAIM: release failed for session=%s (%s); the TTL will expire it",
            claim.session_id,
            exc,
        )
