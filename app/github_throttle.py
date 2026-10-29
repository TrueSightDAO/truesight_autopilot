"""A single global bound on Sophia's concurrent outbound GitHub API calls.

Why this exists (plan: SOPHIA_GITHUB_RATE_LIMIT_GOVERNOR_PLAN.md, context
section): thread concurrency is already shipped, so several Telegram topics run
turns in parallel. Tool bodies execute on ``asyncio.to_thread`` workers, which
means their GitHub reads and writes now burst the *same* PAT simultaneously.
Nothing bounded that concurrency, so the PAT could be driven to 403 even when
the total calls per hour were well inside the 5,000/h budget -- the failure was
pacing, not volume.

The bound must be FLEET-wide, not per-process
---------------------------------------------
The first version of this module created one ``threading.BoundedSemaphore`` at
import time. That is a *per-OS-process* bound. Sophia now runs
``uvicorn --workers 4`` (live since 2026-10-29), so four processes each built
their own semaphore and the fleet permitted **4 x 4 = 16** concurrent calls
against **one** PAT. The bound still looked correct inside every process while
being wrong for the actual deployment -- the exact silent-failure shape this
module exists to prevent. (Reconciliation: ``plans/SOPHIA_DISPATCH_
RECONCILIATION.md``, unit PR-C.)

So the real permit now lives in Redis and every worker claims from the same
allowance. The per-process semaphore is kept as a *secondary* layer: it costs
nothing, and it is what still bounds one process when Redis is unreachable.

Two layers, deliberately:

1. **Global budget (Redis -- the real bound).** An atomic Lua
   "increment-if-below-limit" counter at ``sophia:gh:inflight``. Fleet-wide
   permits stay ``GITHUB_MAX_CONCURRENCY`` (default 4) -- only the *scope*
   changed, from per-worker to per-fleet.
2. **Local semaphore (in-process -- fallback).** Preserves the previous
   behaviour when Redis is down.

Lease, and why a global counter needs one
-----------------------------------------
A global counter is more dangerous than an in-process semaphore if a holder
dies: a leaked token blocks a peer *in another process*, and nothing in the
fleet knows to clear it. Every successful acquire therefore refreshes a
``PEXPIRE`` lease (``GITHUB_THROTTLE_LEASE_MS``, default 120s). If every holder
dies without releasing, the counter expires and the budget self-heals. The lease
is refreshed on each acquire, so a busy live fleet never loses its counter, and
it is comfortably longer than the acquire timeout so a token cannot expire while
its holder is still legitimately waiting.

Acquire timeout / fail-open
---------------------------
``github_throttle()`` waits at most ``GITHUB_THROTTLE_TIMEOUT_SECS`` (default
30s) for a global permit. On timeout it logs a WARNING and lets the caller
proceed *without* one. That is a deliberate trade: this module exists to stop a
403 storm, and it must not introduce a NEW way for a turn to hang forever.
Bounding normal bursts is the win; a pathological stall degrades to
bounded-per-process behaviour, loudly, rather than deadlocking a governor's
thread. Every path releases in ``finally``, so tokens cannot leak on an
exception.

Redis failures are fail-open *and* cheap: after a failure the client is not
retried for ``_REDIS_RETRY_COOLDOWN_S``, so an outage cannot add a
socket-connect stall to every GitHub call for the rest of a turn.

Env:
    GITHUB_MAX_CONCURRENCY          fleet-wide permits (default 4, floor 1)
    GITHUB_THROTTLE_TIMEOUT_SECS    acquire timeout (default 30, floor 1)
    GITHUB_THROTTLE_LEASE_MS        global-counter lease (default 120000)
    GITHUB_THROTTLE_BUDGET_KEY      Redis key override (default
                                    ``sophia:gh:inflight``). Tests set a unique
                                    key so they can never perturb production.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_ACQUIRE_TIMEOUT_SECS = 30.0
DEFAULT_BUDGET_KEY = "sophia:gh:inflight"
DEFAULT_LEASE_MS = 120_000

# Poll schedule while waiting for a fleet-wide permit. Short first delay so a
# freed token is picked up promptly; capped so a long wait is not a hot loop.
_POLL_MIN_S = 0.02
_POLL_MAX_S = 0.5

# After a Redis failure, stop retrying for a while. Without this, an outage adds
# socket_connect_timeout to EVERY GitHub call for the rest of the turn, which is
# a worse failure than the one this module prevents.
_REDIS_RETRY_COOLDOWN_S = 30.0


def _env_number(name: str, default, cast=float):
    """Read a positive number from env, falling back loudly on garbage.

    Missing -> default silently (the common case). Present but unusable -> warn
    and use the default, so a typo can never disable the governor or, worse,
    set the permit count to 0 and block every GitHub call forever.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = cast(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a valid number; using default %s", name, raw, default
        )
        return default
    if val < 1:
        logger.warning("%s=%s must be >= 1; using default %s", name, val, default)
        return default
    return val


def max_concurrency() -> int:
    """Fleet-wide permit count (shared by every worker via Redis)."""
    return int(_env_number("GITHUB_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, int))


def acquire_timeout() -> float:
    """Seconds to wait for a permit before failing open."""
    return _env_number(
        "GITHUB_THROTTLE_TIMEOUT_SECS", DEFAULT_ACQUIRE_TIMEOUT_SECS, float
    )


def budget_key() -> str:
    """Redis key holding the fleet-wide in-flight count."""
    raw = os.environ.get("GITHUB_THROTTLE_BUDGET_KEY") or DEFAULT_BUDGET_KEY
    return raw.strip() or DEFAULT_BUDGET_KEY


def lease_ms() -> int:
    """Lease on the fleet-wide counter, in milliseconds."""
    return int(_env_number("GITHUB_THROTTLE_LEASE_MS", float(DEFAULT_LEASE_MS), float))


# --- Redis budget primitives ------------------------------------------------
# Increment-if-below-limit, atomically. A GET-then-INCR from Python would let N
# workers all read "3 in flight" and all increment, which is the overrun we are
# here to prevent -- the check and the increment must happen inside Redis.
_ACQUIRE_LUA = """
local cur = tonumber(redis.call("GET", KEYS[1]) or "0")
if cur < tonumber(ARGV[1]) then
    redis.call("INCR", KEYS[1])
    redis.call("PEXPIRE", KEYS[1], ARGV[2])
    return 1
end
return 0
"""

# Decrement, floored at zero. A DECR without the floor could go negative if a
# release ever ran twice (e.g. a future refactor releasing in two places), and a
# negative counter would silently hand out EXTRA permits to the fleet.
_RELEASE_LUA = """
local cur = tonumber(redis.call("GET", KEYS[1]) or "0")
if cur > 0 then
    return redis.call("DECR", KEYS[1])
end
return 0
"""

# Local fallback layer. Sized once at import, from the same env var, so a single
# process can never exceed the fleet-wide number on its own.
_local = threading.BoundedSemaphore(max_concurrency())

_client: Any = None
_client_lock = threading.Lock()
_redis_retry_at = 0.0


def _get_client() -> Any:
    """Shared lazy Redis client, with a cooldown after a connection failure."""
    global _client, _redis_retry_at
    if _client is not None:
        return _client
    if time.monotonic() < _redis_retry_at:
        raise RuntimeError("Redis budget client is in post-failure cooldown")
    with _client_lock:
        if _client is not None:
            return _client
        from .redis_claim import get_client  # reuse vault-backed credentials

        try:
            client = get_client()
        except Exception as exc:
            _redis_retry_at = time.monotonic() + _REDIS_RETRY_COOLDOWN_S
            raise RuntimeError(f"could not build Redis client: {exc}") from exc
        _client = client
        return _client


def _note_redis_failure(exc: Exception, what: str) -> None:
    """Log once and start the cooldown so an outage cannot slow every call."""
    global _client, _redis_retry_at
    _redis_retry_at = time.monotonic() + _REDIS_RETRY_COOLDOWN_S
    _client = None
    logger.warning(
        "GitHub concurrency governor: %s failed (%s: %s) -- falling back to the "
        "per-process bound for the next %.0fs",
        what,
        type(exc).__name__,
        exc,
        _REDIS_RETRY_COOLDOWN_S,
    )


def _try_acquire_global() -> bool:
    """One non-blocking attempt at a fleet-wide permit. True iff we won one."""
    client = _get_client()
    won = client.eval(
        _ACQUIRE_LUA, 1, budget_key(), int(max_concurrency()), int(lease_ms())
    )
    return bool(won)


def _acquire_global(timeout: float) -> bool:
    """Poll for a fleet-wide permit, giving up (fail-open) after *timeout*."""
    deadline = time.monotonic() + timeout
    delay = _POLL_MIN_S
    while True:
        try:
            if _try_acquire_global():
                return True
        except Exception as exc:  # noqa: BLE001 -- never break a turn
            _note_redis_failure(exc, "acquire")
            return False
        if time.monotonic() >= deadline:
            logger.warning(
                "GitHub concurrency governor: no fleet-wide permit after %.0fs "
                "(GITHUB_MAX_CONCURRENCY=%d) -- proceeding WITHOUT one "
                "(fail-open) to avoid stalling the turn",
                timeout,
                max_concurrency(),
            )
            return False
        time.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_S)


def _release_global() -> None:
    """Return a fleet-wide permit. Best-effort; the lease is the backstop."""
    try:
        client = _get_client()
        client.eval(_RELEASE_LUA, 1, budget_key())
    except Exception as exc:  # noqa: BLE001 -- release must never raise
        _note_redis_failure(exc, "release")


def global_inflight() -> int | None:
    """Fleet-wide in-flight GitHub calls, or None if Redis is unreadable."""
    try:
        client = _get_client()
        raw = client.get(budget_key())
        return int(raw or 0)
    except Exception as exc:  # noqa: BLE001 -- observability only
        logger.debug("GitHub global in-flight unreadable: %s", exc)
        return None


def remaining_globally() -> int | None:
    """Fleet-wide permits currently free, or None if Redis is unreadable."""
    inflight = global_inflight()
    if inflight is None:
        return None
    return max(0, max_concurrency() - inflight)


def remaining_permits() -> int:
    """Permits free in THIS process's fallback semaphore.

    Deliberately still the per-process view: it is what a single process can
    know without a round trip, and callers use it for cheap assertions. For the
    fleet-wide number use ``remaining_globally()``.
    """
    return _local._value


@contextlib.contextmanager
def github_throttle() -> Iterator[None]:
    """Hold a fleet-wide GitHub permit (and a local one) for the block.

    Wrap only the network call, never surrounding business logic, so permits are
    held for milliseconds-to-seconds instead of for a whole tool round. The
    global permit is taken FIRST: it is the scarcer resource, and waiting for it
    while holding a local permit would tie up a local slot for up to the acquire
    timeout for no benefit.
    """
    timeout = acquire_timeout()
    held_global = _acquire_global(timeout)
    acquired_local = _local.acquire(timeout=timeout)
    if not acquired_local:
        # Fail open rather than hang: see the module docstring's trade-off.
        logger.warning(
            "GitHub concurrency governor: no local permit after %.0fs "
            "(max_concurrency=%d) -- proceeding WITHOUT one (fail-open) to "
            "avoid stalling the turn",
            timeout,
            max_concurrency(),
        )
    try:
        yield
    finally:
        if acquired_local:
            _local.release()
        if held_global:
            _release_global()
