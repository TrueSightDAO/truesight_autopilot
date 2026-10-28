"""Keep a chat turn's deploy_watcher lease alive while the turn is running.

Background (P0 of the loop-daemon program, see
``agentic_ai_context/plans/LOOP_DAEMON_PLAN.md``):

``deploy_watcher.register_track`` stamps ``started_at`` *and* ``last_heartbeat``
with the same instant, and ``can_deploy`` treats a track as blocking only while
``now - last_heartbeat < expected_max_duration_s``. A ``telegram_chat`` track
gets 120s.

``main.py`` registers a chat-turn track when a turn starts and unregisters it in
``finally`` -- but it never called ``deploy_watcher.heartbeat()``. Nothing else
refreshed ``last_heartbeat`` either, so it stayed frozen at ``started_at`` for
the whole turn. Two consequences:

1. **Deploy gate blind spot.** Any turn running longer than 120s stopped
   blocking ``can_deploy``, so a restart could be authorised *mid-turn* and kill
   it -- exactly the long turns this workflow routinely runs (15-30 tool rounds).
2. **False 'stalled' signal.** The loop daemon's classifier reads the same
   ``last_heartbeat``; a healthy 5-minute turn would report as a stalled lease.

This module fixes both by refreshing the lease from a single background task for
the duration of the turn, and refreshing it once more immediately before the
track is unregistered so the expiry can never be back-dated into the turn.

Design notes:

* One task per session, so ``deploy_watcher``'s whole-file read-modify-write
  (it rewrites ``active_tracks.json`` on every call) is driven by at most one
  writer per session. It can still race another *session's* heartbeat, or a
  non-chat track's write, and lose a refresh -- which is harmless: a refresh is
  a monotonic timestamp nudge and losing one costs at most one interval.
* The tick is clamped to at most half the lease, so a caller (or a future
  config change) can never set an interval that would let the lease lapse
  between beats.
* ``heartbeat()`` is itself best-effort, but a *thrown* error here would kill the
  ticker silently. Swallow and keep beating: a dead ticker is worse than a noisy
  log line.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from .deploy_watcher import TRACK_TIMEOUTS, heartbeat

logger = logging.getLogger(__name__)

# Default refresh cadence. Comfortably inside the 120s ``telegram_chat`` lease,
# so a single missed beat cannot expire the track.
DEFAULT_INTERVAL_S = 30.0

_tasks: dict[str, asyncio.Task] = {}


def interval_for(track_type: str, *, default: float = DEFAULT_INTERVAL_S) -> float:
    """Return a refresh interval guaranteed to be inside ``track_type``'s lease.

    Clamped to at most half the lease so ``heartbeat`` always fires twice before
    a track could be considered expired, and floored so a nonsense timeout can
    never produce a zero (busy-spin) interval.
    """
    lease = TRACK_TIMEOUTS.get(track_type)
    if not isinstance(lease, (int, float)) or lease <= 0:
        return default
    return max(1.0, min(default, float(lease) / 2.0))


def _interval_from_env() -> float:
    raw = os.getenv("CHAT_TURN_HEARTBEAT_INTERVAL_S")
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid CHAT_TURN_HEARTBEAT_INTERVAL_S=%r, using %ss",
            raw,
            DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    if value <= 0:
        logger.warning(
            "Non-positive CHAT_TURN_HEARTBEAT_INTERVAL_S=%r, using %ss",
            raw,
            DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    return value


async def _beat_forever(
    track_id: str,
    interval: float,
    beat: Callable[[str], None],
) -> None:
    """Refresh ``track_id``'s lease every ``interval`` seconds until cancelled."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                beat(track_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a beat must never kill the ticker
                logger.debug("heartbeat for %s failed (%s)", track_id, e)
    except asyncio.CancelledError:
        raise


def start(
    track_id: str,
    *,
    interval: float | None = None,
    beat: Callable[[str], None] | None = None,
    ticker: Callable[..., object] | None = None,
) -> asyncio.Task | None:
    """Start refreshing ``track_id``'s lease. Idempotent per ``track_id``.

    ``beat`` and ``ticker`` are injectable for tests. If no running loop is
    available this is a no-op returning ``None`` -- a heartbeat must never be the
    reason a turn fails to start.
    """
    if track_id in _tasks:
        return _tasks[track_id]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop; heartbeat ticker not started for %s", track_id)
        return None
    resolved_interval = interval if interval is not None else _interval_from_env()
    coro = (ticker or _beat_forever)(track_id, resolved_interval, beat or heartbeat)
    task = loop.create_task(coro, name=f"turn-heartbeat:{track_id}")
    _tasks[track_id] = task
    return task


def stop(track_id: str) -> None:
    """Cancel the ticker for ``track_id``. Safe to call more than once."""
    task = _tasks.pop(track_id, None)
    if task is not None and not task.done():
        task.cancel()


def is_running(track_id: str) -> bool:
    """True while a live ticker exists for ``track_id`` (introspection/tests)."""
    task = _tasks.get(track_id)
    return task is not None and not task.done()


async def shutdown() -> None:
    """Cancel every ticker and wait for them to unwind (test/lifespan helper)."""
    tasks = [t for t in _tasks.values() if not t.done()]
    _tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
