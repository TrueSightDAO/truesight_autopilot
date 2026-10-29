"""Per-worker identity registry, heartbeat and session->worker affinity (PR-A).

Scope, stated plainly
---------------------
``plans/SOPHIA_DISPATCH_RECONCILIATION.md`` section 3 settles what "deliberate
dispatch" can mean under the substrate we actually run. ``uvicorn --workers N``
shares one listening socket and the OS kernel picks which child ``accept()``s
each connection, so the application cannot pin an inbound request to a worker
before it is accepted. Dispatch therefore has to mean **affinity routing after
accept**, and that needs three things that did not exist:

1. **identity** -- every worker knows who it is and records it in Redis;
2. **liveness** -- a heartbeat with a 30s TTL, so "is this worker alive" is
   readable by every process (and by the dashboard) instead of being guessed
   from ``/proc``;
3. **affinity** -- a TTL'd record of which worker last accepted a session.

This module is exactly those three, and it is deliberately **observe-only**.
Nothing here routes, defers, hands off, or refuses a turn. PR-F (the actual
wrong-child handoff through ``session_state``) is a separate, human-gated unit
that *consumes* what this writes.

Why the heartbeat is NOT singleton-guarded
------------------------------------------
Nine background loops in ``app/main.py`` are wrapped in
``singleton_guard.run_as_singleton`` because duplicates post duplicate digests.
This loop is the exact opposite: ``run_as_singleton`` would let exactly ONE
worker register, and a registry listing one worker out of four is worse than no
registry at all. So ``heartbeat_loop()`` runs **un-guarded in every worker, by
design**, and ``tests/test_dispatch_registry.py`` asserts that a heartbeat
creates no ``singleton:`` key.

Key scheme
----------
``sophia:worker:<worker_id>``
    Heartbeat record (JSON string), TTL :data:`HEARTBEAT_TTL_SECS`.
``sophia:workers``
    ZSET index: member ``<worker_id>``, score = epoch of last beat. Exists so
    enumeration never needs ``KEYS``/``SCAN`` over an unbounded keyspace --
    ``workers()`` reads N members and MGETs them. Membership deliberately
    outlives the heartbeat, so a dead worker stays *visible as dead* instead of
    silently vanishing; ``prune_stale()`` reaps it later.
``sophia:affinity:<session_id>``
    The worker that last accepted this session, TTL :data:`AFFINITY_TTL_SECS`.
    Long-lived on purpose: it is the history PR-F will route against, not a
    point-in-time signal.

Design rules (same discipline as ``app/session_state.py``)
----------------------------------------------------------
* **Default OFF.** ``DISPATCH_REGISTRY_ENABLED`` unset -> :func:`enabled` is
  False, every writer no-ops, and no Redis client is ever constructed. The
  deploy is behaviour-neutral until a governor flips the flag.
* **Nothing here may raise into a turn or an endpoint.** Redis down, slow, or
  holding nonsense -> log once, return a safe falsy value. A monitoring surface
  that 500s when the thing it monitors is unhealthy is worse than useless.
* **``worker_id`` is ``<host>:<pid>``**, reusing
  ``singleton_guard.default_worker_id()`` rather than re-inventing it, so both
  systems name a process identically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
from typing import Any

from . import redis_claim
from .singleton_guard import default_worker_id

logger = logging.getLogger(__name__)

#: Heartbeat record key prefix (one key per worker).
WORKER_KEY_PREFIX = "sophia:worker:"

#: ZSET index of every worker ever seen; score = epoch of last beat.
WORKERS_INDEX = "sophia:workers"

#: Session -> worker affinity key prefix.
AFFINITY_KEY_PREFIX = "sophia:affinity:"

#: A worker whose last beat is older than this is reported as dead.
HEARTBEAT_TTL_SECS = 30

#: How often each worker beats. Comfortably inside the TTL so one dropped beat
#: never flaps a healthy worker into "dead".
HEARTBEAT_INTERVAL_SECS = 10.0

#: Affinity outlives a single turn by a wide margin: it is routing history, not
#: a liveness signal. Seven days.
AFFINITY_TTL_SECS = 604800

#: Dead workers stay visible this long, then are reaped from the index.
PRUNE_AFTER_SECS = 300

#: Reap every N beats rather than every beat -- pruning is housekeeping.
PRUNE_EVERY_BEATS = 30

#: Process start time, captured at import.
_STARTED_AT = time.time()

#: This process's first beat (set lazily on the first successful heartbeat call).
_FIRST_BEAT: float | None = None

#: The ONLY worker fields allowed to leave the process.
#:
#: ``worker_id`` is ``<host>:<pid>`` -- operational detail that is fine inside
#: the fleet and wrong on a public board. It is projected to ``worker_ref``
#: instead. Iterating this tuple (not the record) makes a future writer field
#: private by default.
PUBLIC_WORKER_FIELDS: tuple[str, ...] = (
    "worker_ref",
    "alive",
    "age_secs",
    "beat_at",
    "first_beat_at",
)

#: True once we have logged a Redis failure, so a broken Redis cannot spam.
_warned = False


def enabled() -> bool:
    """True when the worker registry is switched on (default: off)."""
    return os.environ.get("DISPATCH_REGISTRY_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _warn(exc: Exception) -> None:
    """Log a Redis failure once per process, then stay quiet."""
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "dispatch registry unavailable (%s: %s) -- worker identity/heartbeat is "
        "NOT being recorded; dispatch stays single-worker-shaped",
        type(exc).__name__,
        exc,
    )


def worker_key(worker_id: str) -> str:
    """Redis key holding *worker_id*'s heartbeat record."""
    return f"{WORKER_KEY_PREFIX}{worker_id}"


def affinity_key(session_id: str | int) -> str:
    """Redis key holding the worker that last accepted *session_id*."""
    return f"{AFFINITY_KEY_PREFIX}{session_id}"


def worker_ref(worker_id: str) -> str:
    """Stable, non-reversible short reference for a worker id (public-safe)."""
    return hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:8]


def _decode(raw: Any) -> dict | None:
    """Parse a stored JSON record, or ``None`` if it is missing/nonsense."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _reset_for_tests() -> None:
    """Drop module-level test state. Tests only."""
    global _warned, _FIRST_BEAT
    _warned = False
    _FIRST_BEAT = None


def heartbeat(
    worker_id: str | None = None,
    client: Any = None,
    *,
    now: float | None = None,
    extra: dict | None = None,
) -> bool:
    """Publish this worker's identity + liveness. True if the write landed.

    Writes the TTL'd record and the index score in one pipeline, so a reader can
    never see a member without a record or vice versa for longer than one
    round-trip.
    """
    if not enabled():
        return False

    global _FIRST_BEAT

    who = worker_id or default_worker_id()
    ts = float(now if now is not None else time.time())
    if _FIRST_BEAT is None:
        _FIRST_BEAT = ts

    record: dict[str, Any] = {
        "worker_id": who,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "first_beat_at": _FIRST_BEAT,
        "beat_at": ts,
    }
    if extra:
        record.update({k: v for k, v in extra.items()})

    try:
        cli = client or redis_claim.get_client()
        pipe = cli.pipeline()
        pipe.set(worker_key(who), json.dumps(record), ex=HEARTBEAT_TTL_SECS)
        pipe.zadd(WORKERS_INDEX, {who: ts})
        pipe.execute()
    except Exception as ex:  # noqa: BLE001 -- must never raise into a turn
        _warn(ex)
        return False
    return True


def prune_stale(client: Any = None, *, now: float | None = None) -> int:
    """Reap index members whose last beat is older than :data:`PRUNE_AFTER_SECS`.

    Returns the number removed. Dead workers are kept visible (``alive: False``)
    until they age past this horizon, so the dashboard can show a crash rather
    than an empty list.
    """
    if not enabled():
        return 0
    ts = float(now if now is not None else time.time())
    try:
        cli = client or redis_claim.get_client()
        return int(cli.zremrangebyscore(WORKERS_INDEX, "-inf", ts - PRUNE_AFTER_SECS))
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return 0


async def heartbeat_loop(
    interval: float = HEARTBEAT_INTERVAL_SECS, client: Any = None
) -> None:
    """Beat forever. Runs in EVERY worker -- never wrap this in a singleton.

    ``redis_claim``'s client is the blocking sync one, so each beat hops through
    ``asyncio.to_thread`` exactly like ``session_claim`` does. A failed beat logs
    and the ticker keeps going: the loop must not die because Redis hiccupped,
    or that worker would silently disappear from the registry for good.
    """
    if not enabled():
        logger.info("DISPATCH_REGISTRY_ENABLED is off -- worker heartbeat not started")
        return

    beats = 0
    while True:
        try:
            await asyncio.to_thread(heartbeat, None, client)
            beats += 1
            if beats % PRUNE_EVERY_BEATS == 0:
                await asyncio.to_thread(prune_stale, client)
        except Exception as ex:  # noqa: BLE001 -- the loop must survive
            _warn(ex)
        await asyncio.sleep(max(1.0, float(interval)))


def _read_workers(client: Any, ts: float) -> list[dict]:
    """Read and shape the fleet. RAISES on Redis failure; callers decide.

    Kept separate from :func:`workers` so :func:`snapshot` can tell "no workers"
    apart from "Redis is broken". Collapsing both into an empty list would make
    a dead Redis render as a healthy empty fleet -- exactly the lie a dashboard
    must not tell.
    """
    cli = client or redis_claim.get_client()
    members = cli.zrange(WORKERS_INDEX, 0, -1, withscores=True)
    if not members:
        return []
    raws = cli.mget([worker_key(str(m)) for m, _ in members])

    out: list[dict[str, Any]] = []
    for (member, score), raw in zip(members, raws, strict=False):
        record = _decode(raw)
        beat = record.get("beat_at", score) if record else score
        try:
            beat = float(beat)
        except (TypeError, ValueError):
            beat = float(score)
        age = round(max(0.0, ts - beat), 1)
        out.append(
            {
                "worker_id": str(member),
                "alive": age <= HEARTBEAT_TTL_SECS,
                "age_secs": age,
                "beat_at": beat,
                "first_beat_at": (record or {}).get("first_beat_at"),
                "started_at": (record or {}).get("started_at"),
                "host": (record or {}).get("host"),
                "pid": (record or {}).get("pid"),
                "roles": (record or {}).get("roles"),
            }
        )

    # Alive first, then freshest beat first.
    out.sort(key=lambda r: (not r["alive"], r["age_secs"]))
    return out


def workers(client: Any = None, *, now: float | None = None) -> list[dict]:
    """Every worker ever registered, alive ones first. Never raises."""
    ts = float(now if now is not None else time.time())
    try:
        return _read_workers(client, ts)
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return []


def alive_workers(client: Any = None) -> list[dict]:
    """Only the workers whose heartbeat is currently live."""
    return [r for r in workers(client) if r["alive"]]


def worker_count(client: Any = None) -> dict[str, int]:
    """``{"total": N, "alive": A, "dead": D}``. Never raises."""
    try:
        records = workers(client)
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return {"total": 0, "alive": 0, "dead": 0}
    alive = sum(1 for r in records if r["alive"])
    return {"total": len(records), "alive": alive, "dead": len(records) - alive}


def set_affinity(
    session_id: str | int,
    worker_id: str | None = None,
    client: Any = None,
) -> bool:
    """Record that *worker_id* accepted *session_id*. True if the write landed.

    WRITTEN ONLY. No caller routes on this yet -- see the module docstring.
    """
    if not enabled():
        return False
    who = worker_id or default_worker_id()
    try:
        cli = client or redis_claim.get_client()
        cli.set(affinity_key(session_id), who, ex=AFFINITY_TTL_SECS)
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return False
    return True


def affinity_owner(session_id: str | int, client: Any = None) -> str | None:
    """The worker that last accepted *session_id*, or ``None``. Never raises."""
    if not enabled():
        return None
    try:
        cli = client or redis_claim.get_client()
        raw = cli.get(affinity_key(session_id))
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return None
    return str(raw) if raw is not None else None


def clear_affinity(session_id: str | int, client: Any = None) -> bool:
    """Drop *session_id*'s affinity record. True if a key was deleted."""
    if not enabled():
        return False
    try:
        cli = client or redis_claim.get_client()
        return bool(cli.delete(affinity_key(session_id)))
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return False


def note_session_accepted(session_id: str | int) -> None:
    """Fire-and-forget affinity write, safe to ``create_task`` from a turn.

    Deliberately non-blocking: an affinity *record* must never add latency to the
    request it describes, so the write happens off the event loop and every
    failure is swallowed (the reader simply sees no affinity for that session).
    """
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync caller) -- write inline rather than drop it.
        try:
            set_affinity(session_id)
        except Exception as ex:  # noqa: BLE001
            _warn(ex)
        return
    try:
        loop.create_task(asyncio.to_thread(set_affinity, session_id))
    except Exception as ex:  # noqa: BLE001
        _warn(ex)


def _public_worker(record: dict) -> dict[str, Any]:
    """Project one worker record onto :data:`PUBLIC_WORKER_FIELDS`.

    Iterating the allowlist (not the record) is the point: a field added to the
    writer later cannot leak by default.
    """
    out: dict[str, Any] = {
        "worker_ref": worker_ref(str(record.get("worker_id", ""))),
    }
    for field in PUBLIC_WORKER_FIELDS:
        if field == "worker_ref":
            continue
        out[field] = record.get(field)
    return out


def snapshot(client: Any = None, *, now: float | None = None) -> dict[str, Any]:
    """Dashboard-ready, public-safe view of the worker fleet. Never raises."""
    if not enabled():
        return {
            "enabled": False,
            "heartbeat_ttl_secs": HEARTBEAT_TTL_SECS,
            "workers": [],
            "worker_count": 0,
            "alive": 0,
            "dead": 0,
            "degraded": False,
            "reason": "DISPATCH_REGISTRY_ENABLED is off; set it to register workers.",
        }

    ts = float(now if now is not None else time.time())
    try:
        records = _read_workers(client, ts)
    except Exception as ex:  # noqa: BLE001
        _warn(ex)
        return {
            "enabled": True,
            "heartbeat_ttl_secs": HEARTBEAT_TTL_SECS,
            "workers": [],
            "worker_count": 0,
            "alive": 0,
            "dead": 0,
            "degraded": True,
            "reason": "dispatch registry unavailable",
        }

    alive = sum(1 for r in records if r["alive"])
    return {
        "enabled": True,
        "heartbeat_ttl_secs": HEARTBEAT_TTL_SECS,
        "affinity_ttl_secs": AFFINITY_TTL_SECS,
        "workers": [_public_worker(r) for r in records],
        "worker_count": len(records),
        "alive": alive,
        "dead": len(records) - alive,
        "degraded": False,
    }
