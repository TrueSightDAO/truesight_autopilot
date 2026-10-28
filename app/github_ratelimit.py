"""Proactive GitHub rate-limit awareness (plan PR2).

Sophia's write PAT (``TRUESIGHT_DAO_AUTOPILOT``) is a fine-grained token with a
5,000 req/h *core* budget. Once that budget is exhausted GitHub returns ``403``,
and PyGithub's default retry policy then sleeps through ``Retry-After`` for up
to ~25 minutes -- on a ``--workers 1`` process that wedges the whole service.

PR1 (#553) bounded *concurrency*. It does not bound *budget*: four concurrent
requests still spend four of five thousand. So PR1 cannot prevent this failure
mode; this module supplies the missing half -- read the budget *before* firing,
warn when it is low, and wait it out when it is critical, rather than firing
blind into a 403 and letting PyGithub back off for minutes.

GitHub meters these budgets per *user*, not per token: two PATs belonging to
the same account share one 5,000/h pool (verified 2026-10-28 -- all three of
Sophia's PATs reported user ID 1079482). So holding a "dedicated" token does
not buy a second budget; only a different owning account would.

``GET /rate_limit`` is free (it does not consume the core budget), which makes
polling it the cheapest way to learn state for callers that cannot see response
headers -- PyGithub does not surface them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("autopilot.github")

RATE_LIMIT_URL = "https://api.github.com/rate_limit"

# Env-tunable thresholds. Module-level so tests can monkeypatch them directly.
WARN_BELOW = int(os.getenv("GITHUB_RATELIMIT_WARN_BELOW", "200"))
SLEEP_BELOW = int(os.getenv("GITHUB_RATELIMIT_SLEEP_BELOW", "50"))
MAX_SLEEP = float(os.getenv("GITHUB_RATELIMIT_MAX_SLEEP", "60"))
STATE_TTL = float(os.getenv("GITHUB_RATELIMIT_STATE_TTL", "30"))

#: Trip at *true* exhaustion only. Kept at 0 rather than ``SLEEP_BELOW``
#: deliberately: while any budget remains the call can still succeed, so sleeping
#: is wasteful but harmless. It is only once the budget is gone that firing is
#: guaranteed to fail.
BLOCK_BELOW = int(os.getenv("GITHUB_RATELIMIT_BLOCK_BELOW", "0"))


class RateLimitExhausted(RuntimeError):
    """Core budget is gone and the reset is too far out for a capped wait.

    Raised instead of sleeping-and-then-403, so a caller fails fast and honestly
    rather than spending a minute to earn a guaranteed error.
    """

    def __init__(self, remaining: int, reset_in: float) -> None:
        self.remaining = remaining
        self.reset_in = reset_in
        super().__init__(
            "GitHub core rate limit exhausted "
            f"({remaining} remaining, resets in {reset_in:.0f}s)"
        )


_lock = threading.Lock()
_state: dict[str, Any] = {"remaining": None, "reset": None, "checked_at": 0.0}


def _parse(payload: dict[str, Any]) -> tuple[int | None, float | None]:
    """Pull (remaining, reset) for the ``core`` bucket out of a /rate_limit body."""
    core = (payload.get("resources") or {}).get("core") or {}
    remaining = core.get("remaining")
    reset = core.get("reset")
    return (
        int(remaining) if remaining is not None else None,
        float(reset) if reset is not None else None,
    )


def reset_state() -> None:
    """Clear cached budget state (tests, and after a token change)."""
    with _lock:
        _state.update(remaining=None, reset=None, checked_at=0.0)


def snapshot() -> dict[str, Any]:
    """Current cached budget state, for diagnostics / tests."""
    with _lock:
        return dict(_state)


def refresh(
    pat: str, *, force: bool = False, now_fn=None
) -> tuple[int | None, float | None]:
    """Refresh (remaining, reset) by probing the free /rate_limit endpoint.

    Reuses cached state for ``STATE_TTL`` seconds so a burst of calls costs one
    probe, not one per call. Never raises: a probe failure leaves the previous
    state in place and returns it, because a rate-limit *check* must never be
    the reason a GitHub operation fails.
    """
    now_fn = now_fn or time.time
    with _lock:
        fresh = (
            _state["remaining"] is not None
            and (now_fn() - _state["checked_at"]) < STATE_TTL
        )
    if fresh and not force:
        with _lock:
            return _state["remaining"], _state["reset"]

    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if pat:
            headers["Authorization"] = f"Bearer {pat}"
        resp = httpx.get(RATE_LIMIT_URL, headers=headers, timeout=10.0)
        remaining, reset = _parse(resp.json())
    except Exception:
        logger.debug("rate-limit probe failed", exc_info=True)
        with _lock:
            return _state["remaining"], _state["reset"]

    with _lock:
        _state.update(remaining=remaining, reset=reset, checked_at=now_fn())
    return remaining, reset


def wait_if_exhausted(pat: str, *, now_fn=None, sleep_fn=None) -> float:
    """Warn on a low core budget; wait (capped) when it is critical.

    Returns the number of seconds slept (0.0 when no wait was needed), so
    callers and tests can observe the decision rather than infer it.
    """
    now_fn = now_fn or time.time
    sleep_fn = sleep_fn or time.sleep

    remaining, reset = refresh(pat, now_fn=now_fn)
    if remaining is None:
        return 0.0

    if remaining < WARN_BELOW:
        logger.warning(
            "GitHub core budget low: %s remaining (resets in %.0fs)",
            remaining,
            max(0.0, (reset or now_fn()) - now_fn()),
        )

    if remaining >= SLEEP_BELOW or reset is None:
        return 0.0

    seconds_to_reset = max(reset - now_fn(), 0.0)

    # Budget fully gone. A capped sleep cannot bridge a reset that is further out
    # than MAX_SLEEP, so sleeping spends a minute only to fire into a 403 anyway
    # -- observed in production as a sleep/403 loop from 09:49 to 09:51 on
    # 2026-10-28 (0 remaining, resets in 1091s, MAX_SLEEP=60). Fail fast so the
    # caller's own handler turns this into an honest error.
    if remaining <= BLOCK_BELOW and seconds_to_reset > MAX_SLEEP:
        logger.error(
            "GitHub core budget exhausted (%s remaining, resets in %.0fs) -- "
            "failing fast rather than sleeping %.0fs for a guaranteed 403",
            remaining,
            seconds_to_reset,
            MAX_SLEEP,
        )
        raise RateLimitExhausted(remaining=remaining, reset_in=seconds_to_reset)

    delay = min(seconds_to_reset + 1.0, MAX_SLEEP)
    if delay <= 0:
        return 0.0
    logger.warning(
        "GitHub core budget exhausted (%s remaining) -- waiting %.0fs before the call",
        remaining,
        delay,
    )
    sleep_fn(delay)
    return delay
