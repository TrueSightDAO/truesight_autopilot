"""Layer-2 cross-process session claim (defense in depth over the asyncio.Lock).

Why this module exists (``plans/SOPHIA_SIDEKIQ_MULTIPROCESS_PLAN.md``, PR1):

``app/main.py`` serialises a conversation with ``_session_lock`` -- an
``asyncio.Lock`` per ``session_key``. That is a *layer-1* guarantee and it is
real, but it lives inside ONE OS process's memory. The moment uvicorn runs with
``--workers N`` (N > 1), two workers each hold their own lock for the same
session and can run the same thread concurrently, racing on the shared
transcript file -- the failure that bricked threads 3 and 780 on 2026-06-10.

This module adds the *layer-2* guard: the Redis claim primitives that
``app/redis_claim.py`` already shipped (``try_claim`` / ``release`` / ``renew``),
keyed on a ``session_id`` and held for the duration of one turn. It is
deliberately **layered, not a replacement**:

* layer 1 (``asyncio.Lock``, always on) closes the intra-process race and keeps
  working when Redis is down;
* layer 2 (this module, opt-in) closes the *inter-process* race.

KNOWN LIMITATION (KL-1 -- flagged by the governor, do not paper over it): the
conjunction "Redis unreachable AND ``--workers > 1`` live" is NOT closed by this
layering. Each process would fall back to its own layer-1 lock and two processes
could still race on one thread. Layer 2 therefore ships DEFAULT-OFF
(``SESSION_CLAIM_ENABLED``) because workers stays at 1 until the plan's PR5/PR6
human gate, so that window is unreachable today. Revisit explicitly at PR4
(load test) and PR5 (go-ahead); the likely fix then is fail-closed (refuse the
turn as "session busy" when Redis is unreachable and multi-worker is declared).

Design notes:

* **Reusing, not re-implementing, the primitives.** Keys are built as
  ``lock:session:<session_id>`` through ``redis_claim.lock_key``, so the shipped
  atomic ``SET NX EX`` and the check-owner-then-act Lua run unchanged. The extra
  ``session:`` namespace keeps this disjoint from ``lock:<int thread_id>``.
* **The holder token IS part of the worker_id** (``"<host>:<pid>|<token>"``).
  ``main`` mints a fresh uuid token per acquisition precisely so a stale holder
  resuming later cannot free a newer holder's lock; embedding the token means
  that property survives the process boundary too. A bare process id would have
  quietly broken it.
* **Renewal is required, not a nicety.** ``DEFAULT_CLAIM_TTL_SECS`` (3600s) is
  *shorter* than the inner lock's stale window (5180s), so a legitimate long turn
  would lose its claim mid-work. A background renewer extends the TTL only while
  we still own the key (``turn_heartbeat``'s shape: one task per session,
  clamped interval, a failed beat never kills the ticker).
* **Nothing here may fail or stall a turn.** ``redis_claim`` uses the blocking
  sync Redis client, so every call hops through ``asyncio.to_thread``; every
  failure path degrades to layer-1-only with a loud log. ``enabled()``
  short-circuits *before* a client is ever constructed, so the default-OFF path
  cannot touch Redis at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

from . import redis_claim

logger = logging.getLogger(__name__)

# Prepended to the session id before it reaches ``redis_claim.lock_key``, which
# adds ``lock:`` -- final key ``lock:session:<session_id>``.
CLAIM_KEY_PREFIX = "session:"

DEFAULT_TTL_SECS = redis_claim.DEFAULT_CLAIM_TTL_SECS

# How long a turn waits for a peer process' claim before degrading to
# layer-1-only. Bounded on purpose: the caller (``_session_lock_guard``) is
# holding a live HTTP request, so waiting forever is not an option.
DEFAULT_WAIT_SECS = 120.0

# Renewal cadence ceiling, clamped to ``ttl / 3`` so two beats can be missed
# before the claim could lapse.
DEFAULT_RENEW_INTERVAL_S = 300.0

_POLL_MIN_S = 0.25
_POLL_MAX_S = 5.0

_renewers: dict[str, asyncio.Task] = {}
_process_id: str | None = None


@dataclass(frozen=True)
class Claim:
    """A won cross-process claim: who holds it, on what key, for how long."""

    session_id: str
    worker_id: str
    ttl: int


def _env_positive_float(name: str, default: float) -> float:
    """Read a positive float override, logging loudly and falling back if junk."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.error("%s=%r is not a number -- using %.0fs", name, raw, default)
        return default
    if value <= 0:
        logger.error("%s=%r is not > 0 -- using %.0fs", name, raw, default)
        return default
    return value


def enabled() -> bool:
    """Whether layer 2 is switched on. Default False -- see KL-2 in the PR body."""
    return os.environ.get("SESSION_CLAIM_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ttl_secs() -> int:
    return int(_env_positive_float("SESSION_CLAIM_TTL_SECS", float(DEFAULT_TTL_SECS)))


def wait_secs() -> float:
    return _env_positive_float("SESSION_CLAIM_WAIT_SECS", DEFAULT_WAIT_SECS)


def renew_interval(ttl: int) -> float:
    """Renewal cadence for a *ttl*-second claim: never stalls past ``ttl / 3``."""
    return max(1.0, min(DEFAULT_RENEW_INTERVAL_S, float(ttl) / 3.0))


def process_id() -> str:
    """``<hostname>:<pid>``, cached -- the process half of a worker id."""
    global _process_id
    if _process_id is None:
        try:
            host = socket.gethostname() or "unknown-host"
        except OSError:
            host = "unknown-host"
        _process_id = f"{host}:{os.getpid()}"
    return _process_id


def worker_id(token: str) -> str:
    """Build the claim owner id, embedding the per-acquisition holder token."""
    if not token:
        raise ValueError("a per-acquisition holder token is required")
    return f"{process_id()}|{token}"


def claim_key(session_id: str) -> str:
    """The Redis key a *session_id* claim lives on."""
    return redis_claim.lock_key(_ns(session_id))


def _ns(session_id: str) -> str:
    """Namespace a session id for ``redis_claim`` (which prefixes ``lock:``)."""
    return f"{CLAIM_KEY_PREFIX}{session_id}"


async def _owner_or_unknown(session_id: str, client: Any = None) -> str:
    try:
        return (
            await asyncio.to_thread(redis_claim.owner_of, _ns(session_id), client)
            or "<unknown>"
        )
    except Exception as e:  # noqa: BLE001 -- diagnostics only, never fatal
        logger.debug("SESSION CLAIM: owner lookup failed (%s)", e)
        return "<unknown>"


async def acquire(
    session_id: str,
    token: str,
    *,
    wait: float | None = None,
    ttl: int | None = None,
    client: Any = None,
) -> Claim | None:
    """Try to win the cross-process claim for *session_id*.

    Returns a ``Claim`` on success. Returns ``None`` when layer 2 is disabled,
    when Redis is unavailable, or when a peer held the session for longer than
    the (bounded) wait -- in every one of those cases the caller continues on
    layer 1 alone, and the reason is logged rather than swallowed.
    """
    if not enabled():
        return None

    wid = worker_id(token)
    effective_ttl = ttl if ttl is not None else ttl_secs()
    budget = wait if wait is not None else wait_secs()
    deadline = time.monotonic() + budget
    delay = _POLL_MIN_S
    announced = False

    while True:
        try:
            won = await asyncio.to_thread(
                redis_claim.try_claim, _ns(session_id), wid, effective_ttl, client
            )
        except Exception as e:  # noqa: BLE001 -- Redis must never break a turn
            logger.error(
                "SESSION CLAIM: Redis unavailable claiming session=%s (%s) -- "
                "degrading to the layer-1 in-process lock only (KL-1)",
                session_id,
                e,
            )
            return None

        if won:
            return Claim(session_id=session_id, worker_id=wid, ttl=effective_ttl)

        if not announced:
            logger.warning(
                "SESSION CLAIM: session=%s is claimed by %s -- waiting up to "
                "%.0fs for it to clear",
                session_id,
                await _owner_or_unknown(session_id, client),
                budget,
            )
            announced = True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error(
                "SESSION CLAIM: gave up waiting for session=%s after %.0fs "
                "(holder=%s) -- proceeding DEGRADED on the layer-1 lock only; "
                "a peer process may be running this thread",
                session_id,
                budget,
                await _owner_or_unknown(session_id, client),
            )
            return None

        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, _POLL_MAX_S)


def release(claim: Claim | None, *, client: Any = None) -> bool:
    """Release *claim* if we still own it. Synchronous on purpose.

    Called from ``_session_lock_guard``'s ``finally``, which also runs while an
    async generator is being finalised (the SSE ``/chat`` path) -- an ``await``
    is the one thing a torn-down loop can bite on there. The call is a ~1ms
    localhost round trip, and ``_unregister_chat_turn_track`` already does
    synchronous I/O in that same teardown position.
    """
    if claim is None:
        return False
    try:
        return redis_claim.release(_ns(claim.session_id), claim.worker_id, client)
    except Exception as e:  # noqa: BLE001 -- teardown must never raise
        logger.error(
            "SESSION CLAIM: release failed for session=%s (%s) -- the key will "
            "age out via its TTL instead",
            claim.session_id,
            e,
        )
        return False


def _renew_once(claim: Claim, client: Any = None) -> bool:
    try:
        return redis_claim.renew(
            _ns(claim.session_id), claim.worker_id, claim.ttl, client
        )
    except Exception as e:  # noqa: BLE001 -- a beat must never kill the ticker
        logger.debug("SESSION CLAIM: renew attempt failed (%s)", e)
        return False


async def _renew_forever(
    claim: Claim,
    interval: float,
    renew: Any = None,
) -> None:
    """Extend *claim*'s TTL every *interval* seconds until cancelled or lost."""
    tick = renew or _renew_once
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                still_ours = await asyncio.to_thread(tick, claim)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("SESSION CLAIM: renewal beat failed (%s)", e)
                continue
            if not still_ours:
                logger.error(
                    "SESSION CLAIM: renewal LOST session=%s (claim expired or a "
                    "peer took it) -- stopping the renewer; layer 1 still holds",
                    claim.session_id,
                )
                return
    except asyncio.CancelledError:
        raise


def start_renewer(
    claim: Claim | None,
    *,
    interval: float | None = None,
    renew: Any = None,
) -> asyncio.Task | None:
    """Start the TTL renewer for *claim*. Idempotent per session.

    A no-op returning ``None`` when there is nothing to renew or no running
    loop -- a renewer must never be the reason a turn fails to start.
    """
    if claim is None:
        return None
    sid = claim.session_id
    if sid in _renewers:
        return _renewers[sid]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop; claim renewer not started for %s", sid)
        return None
    resolved = interval if interval is not None else renew_interval(claim.ttl)
    task = loop.create_task(
        _renew_forever(claim, resolved, renew), name=f"session-claim-renew:{sid}"
    )
    _renewers[sid] = task
    return task


def stop_renewer(session_id: str) -> None:
    """Cancel the renewer for *session_id*. Safe to call more than once."""
    task = _renewers.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()


def renewer_running(session_id: str) -> bool:
    """True while a live renewer exists for *session_id* (introspection/tests)."""
    task = _renewers.get(session_id)
    return task is not None and not task.done()


async def owner_of(session_id: str, *, client: Any = None) -> str | None:
    """Current claim owner's worker id for *session_id*, or None if free."""
    try:
        return await asyncio.to_thread(redis_claim.owner_of, _ns(session_id), client)
    except Exception as e:  # noqa: BLE001
        logger.debug("SESSION CLAIM: owner lookup failed for %s (%s)", session_id, e)
        return None


async def shutdown() -> None:
    """Cancel every renewer and wait for them to unwind (test/lifespan helper)."""
    tasks = [t for t in _renewers.values() if not t.done()]
    _renewers.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def reset_process_id_cache() -> None:
    """Test helper: forget the cached ``<host>:<pid>``."""
    global _process_id
    _process_id = None
