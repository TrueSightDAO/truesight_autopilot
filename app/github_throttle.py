"""A single global bound on Sophia's concurrent outbound GitHub API calls.

Why this exists (plan: SOPHIA_GITHUB_RATE_LIMIT_GOVERNOR_PLAN.md, context section):
thread concurrency is already shipped, so several Telegram topics run turns in
parallel. Tool bodies execute on ``asyncio.to_thread`` workers, which means their
GitHub reads and writes now burst the *same* PAT simultaneously. Nothing bounded
that concurrency, so the PAT could be driven to 403 even when the total calls per
hour were well inside the 5,000/h budget -- the failure was pacing, not volume.

One shared semaphore, deliberately in its own tiny module:

The plan's PR1 row names ``github_tools.py`` and ``deploy_ledger.py`` as the
files to guard. Implementing "a module-level semaphore" separately in each of
those files would technically satisfy the wording while defeating the stated
intent ("a *global* bound so the now-parallel threads don't stampede GitHub"):
two independent semaphores of 4 permit 8 concurrent calls, and the two worst
offenders -- the read path and the deploy-ledger path -- would not see each
other at all. Both modules therefore import the one semaphore here.

Acquire timeout / fail-open:

``acquire()`` waits at most ``GITHUB_THROTTLE_TIMEOUT_SECS`` (default 30s). On
timeout it logs a WARNING and lets the caller proceed *without* a permit. That is
a deliberate trade: this module exists to stop a 403 storm, and it must not
introduce a NEW way for a turn to hang forever. Bounding normal bursts is the
win; a pathological stall degrades to the previous unbounded behaviour, loudly,
instead of deadlocking a governor's thread. Every path releases in ``finally``,
so permits cannot leak on an exception.

Env:
    GITHUB_MAX_CONCURRENCY          permits (default 4, floor 1)
    GITHUB_THROTTLE_TIMEOUT_SECS    acquire timeout (default 30, floor 1)
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Iterator

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_ACQUIRE_TIMEOUT_SECS = 30.0


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
    """Permit count for the global GitHub semaphore."""
    return int(_env_number("GITHUB_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, int))


def acquire_timeout() -> float:
    """Seconds to wait for a permit before failing open."""
    return _env_number(
        "GITHUB_THROTTLE_TIMEOUT_SECS", DEFAULT_ACQUIRE_TIMEOUT_SECS, float
    )


# Created once at import. Both github_tools and deploy_ledger import THIS object.
_semaphore = threading.BoundedSemaphore(max_concurrency())


def remaining_permits() -> int:
    """Permits currently free. Used by tests and for observability."""
    return _semaphore._value


@contextlib.contextmanager
def github_throttle() -> Iterator[None]:
    """Hold one global GitHub permit for the duration of the block.

    Wrap only the network call, never surrounding business logic, so permits are
    held for milliseconds-to-seconds instead of for a whole tool round.
    """
    timeout = acquire_timeout()
    acquired = _semaphore.acquire(timeout=timeout)
    if not acquired:
        # Fail open rather than hang: see the module docstring's trade-off.
        logger.warning(
            "GitHub concurrency governor: no permit after %.0fs (max_concurrency=%d) "
            "-- proceeding WITHOUT a permit (fail-open) to avoid stalling the turn",
            timeout,
            max_concurrency(),
        )
        yield
        return
    try:
        yield
    finally:
        _semaphore.release()
