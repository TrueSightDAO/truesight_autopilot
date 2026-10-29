"""Read surface for loop-daemon verdicts (U3): the ``/loop/status`` payload.

The daemon publishes one TTL'd hash per session under ``loop:status:<session_id>``
plus a TTL'd heartbeat (``loop:daemon:heartbeat``). Until now that state could only
be read over SSH with ``redis-cli``, so "why did nothing poke me?" was unanswerable
without a shell. This module turns the published state back into ``SessionStatus``
objects and one report, so the same truth the alert layer acts on is visible as JSON.

Everything here is deliberately best-effort. A Redis blip degrades to a report marked
``degraded`` rather than raising: the whole point of the surface is to *reveal* that
the loop is unhealthy, so failing closed would hide exactly what it exists to show.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import loop_status
from .loop_status import SessionStatus

log = logging.getLogger(__name__)

STATUS_PREFIX = "loop:status:"
DAEMON_KEY = "loop:daemon:heartbeat"

#: The daemon TTLs its own heartbeat at ``interval * TTL_MULTIPLIER`` (60 * 3 = 180s).
#: A heartbeat older than that means the daemon stopped publishing -- worth reporting
#: rather than returning an empty sweep that looks perfectly healthy.
DEFAULT_HEARTBEAT_STALE_S = 180.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_status_hash(raw: Any) -> SessionStatus | None:
    """Rebuild a :class:`SessionStatus` from a published hash.

    Returns ``None`` when the hash is unusable (not a mapping, or missing the
    ``session_id``/``state`` a verdict is meaningless without) so one corrupt key
    cannot poison the whole report.
    """
    if not isinstance(raw, dict):
        return None
    session_id = str(raw.get("session_id") or "").strip()
    state = str(raw.get("state") or "").strip()
    if not session_id or not state:
        return None

    age_s: float | None = None
    raw_age = raw.get("age_s")
    if raw_age not in (None, ""):
        try:
            age_s = float(raw_age)
        except (TypeError, ValueError):
            age_s = None

    return SessionStatus(
        session_id=session_id,
        state=state,
        reason=str(raw.get("reason") or ""),
        age_s=age_s,
        label=str(raw.get("label") or ""),
        goal=str(raw.get("goal") or ""),
    )


def read_statuses(client: Any) -> list[SessionStatus]:
    """Read every published verdict. Sorted so the output is stable across calls."""
    statuses: list[SessionStatus] = []
    for key in client.scan_iter(f"{STATUS_PREFIX}*", count=200):
        try:
            raw = client.hgetall(key)
        except Exception:  # noqa: BLE001 - one bad key must not kill the report
            log.warning("could not read loop status %s", key, exc_info=True)
            continue
        status = parse_status_hash(raw)
        if status is None:
            log.warning("skipping unusable loop status hash %s", key)
            continue
        statuses.append(status)
    statuses.sort(key=lambda s: s.session_id)
    return statuses


def heartbeat_age(client: Any, *, now: float | None = None) -> float | None:
    """Seconds since the daemon last published, or ``None`` if never/unreadable."""
    ts = time.time() if now is None else now
    try:
        raw = client.get(DAEMON_KEY)
    except Exception:  # noqa: BLE001
        log.warning("could not read daemon heartbeat", exc_info=True)
        return None
    if raw in (None, ""):
        return None
    try:
        beat = float(raw)
    except (TypeError, ValueError):
        log.warning("daemon heartbeat is not a timestamp: %r", raw)
        return None
    return max(0.0, ts - beat)


def _defect_payload(status: SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": status.session_id,
        "state": status.state,
        "reason": status.reason,
        "label": status.label,
        "goal": status.goal,
    }
    if status.age_s is not None:
        payload["age_s"] = round(status.age_s, 1)
    return payload


def build_report(
    client: Any = None,
    *,
    now: float | None = None,
    heartbeat_stale_s: float = DEFAULT_HEARTBEAT_STALE_S,
) -> dict[str, Any]:
    """Assemble the ``/loop/status`` document.

    ``healthy`` is true only when the sweep found no defects **and** the daemon is
    still publishing and Redis was readable. A silent daemon with an empty status
    set is the most misleading state possible, so it is explicitly not healthy.
    """
    ts = time.time() if now is None else now
    error: str | None = None
    statuses: list[SessionStatus] = []
    age: float | None = None

    if client is None:
        try:
            from .redis_claim import get_client

            client = get_client()
        except Exception as exc:  # noqa: BLE001
            error = f"redis unavailable: {exc}"

    if client is not None:
        try:
            statuses = read_statuses(client)
        except Exception as exc:  # noqa: BLE001
            error = error or f"status read failed: {exc}"
        age = heartbeat_age(client, now=ts)

    summary = loop_status.summarize(statuses)
    daemon_stale = error is not None or age is None or age > heartbeat_stale_s

    return {
        "generated_at": _iso(ts),
        "total": summary["total"],
        "counts": summary["counts"],
        "defects": [_defect_payload(s) for s in summary["defects"]],
        "daemon": {
            "heartbeat_age_s": None if age is None else round(age, 1),
            "stale": daemon_stale,
            "heartbeat_stale_after_s": heartbeat_stale_s,
        },
        "healthy": bool(summary["healthy"]) and not daemon_stale,
        "degraded": error is not None,
        "error": error,
    }
