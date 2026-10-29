"""Shared per-session state: the incoming message queue and the cancel flag.

Both were module-level dicts in ``app/main.py``, which makes them *process-local*.
At ``workers > 1`` that is a correctness bug, not an optimisation:

* ``_message_queues`` -- an adapter POST routed to worker B while worker A is
  running the turn pushes the message into a list nobody drains. Claim-loss
  therefore means **work loss**, not deferral.
* ``_cancel_flags`` -- a cancel POST can land on a worker that is not running the
  turn, where it is silently ignored, so the user's cancel does nothing.

This module moves both onto Redis so no process *owns* session state and routing
stops mattering.

Key scheme
----------
Every key is ``<kind>:<lock_scope>`` where ``lock_scope`` is the canonical
conversation key from ``_lock_scope`` -- ``tg:<chat>:<thread>`` for Telegram,
``dc:<guild>:<channel>`` for Discord. Deliberately the SAME string the session
lock uses, and deliberately NOT the pubkey-prefixed transcript identity: putting
governor identity into a *durable* shared key would reintroduce the coupling the
lock-scope fix removed (see OPEN_FOLLOWUPS: "Multi-process plan: share session
state").

Design rules
------------
1. **Default OFF.** ``SESSION_STATE_SHARED`` unset -> the in-process dicts serve,
   exactly as before, and no Redis client is ever constructed.
2. **Redis must never break a turn.** Every Redis path is wrapped; on failure we
   log once and fall back to the local dict, degrading to pre-change behaviour
   rather than returning 500 to a chat request.
3. **Pop must be atomic across processes.** ``queue_pop`` uses ``LPOP``, not
   read-then-delete, so two workers cannot both drain the same message.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Fallback state, used when the feature is off OR Redis is unavailable.
_LOCAL_QUEUES: dict[str, list[dict]] = {}
_LOCAL_CANCELS: dict[str, bool] = {}

#: Cancel flags are point-in-time signals; they do not need to outlive a turn.
CANCEL_TTL_SECS = 3600

_client: Any | None = None
_warned = False


def enabled() -> bool:
    """True when shared session state is switched on."""
    return os.environ.get("SESSION_STATE_SHARED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _get_client() -> Any:
    global _client
    if _client is None:
        from . import redis_claim  # local import: keep module import cheap

        _client = redis_claim.get_client()
    return _client


def _reset_for_tests() -> None:
    """Drop the cached client and all local state. Tests only."""
    global _client, _warned
    _client = None
    _warned = False
    _LOCAL_QUEUES.clear()
    _LOCAL_CANCELS.clear()
    _LOCAL_PROGRESS.clear()


def _warn(exc: Exception) -> None:
    """Log a Redis failure once per process, then stay quiet."""
    global _warned
    if not _warned:
        _warned = True
        logger.warning(
            "shared session state unavailable (%s: %s) -- falling back to "
            "process-local dicts; this process is NOT safely shareable",
            type(exc).__name__,
            exc,
        )


def _queue_key(scope: str) -> str:
    return f"queue:{scope}"


def _cancel_key(scope: str) -> str:
    return f"cancel:{scope}"


# --------------------------------------------------------------------------- #
# message queue
# --------------------------------------------------------------------------- #
def queue_push(scope: str, msg: dict) -> int:
    """Append ``msg``; return its 1-based position (the total queue length)."""
    if not enabled():
        local = _LOCAL_QUEUES.setdefault(scope, [])
        local.append(msg)
        return len(local)
    try:
        return int(_get_client().rpush(_queue_key(scope), json.dumps(msg)))
    except Exception as exc:  # noqa: BLE001 - never break a turn
        _warn(exc)
        local = _LOCAL_QUEUES.setdefault(scope, [])
        local.append(msg)
        return len(local)


def queue_len(scope: str) -> int:
    """Number of messages currently queued."""
    if not enabled():
        return len(_LOCAL_QUEUES.get(scope, []))
    try:
        return int(_get_client().llen(_queue_key(scope)))
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        return len(_LOCAL_QUEUES.get(scope, []))


def queue_pop(scope: str) -> dict | None:
    """Atomically take the oldest message, or ``None`` when the queue is empty."""
    if not enabled():
        local = _LOCAL_QUEUES.get(scope) or []
        return local.pop(0) if local else None
    try:
        raw = _get_client().lpop(_queue_key(scope))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        local = _LOCAL_QUEUES.get(scope) or []
        return local.pop(0) if local else None


def queue_snapshot(scope: str) -> list[dict]:
    """Return a copy of the queue, oldest first. Does not consume it."""
    if not enabled():
        return list(_LOCAL_QUEUES.get(scope, []))
    try:
        raw = _get_client().lrange(_queue_key(scope), 0, -1)
        return [json.loads(item) for item in raw]
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        return list(_LOCAL_QUEUES.get(scope, []))


def queue_replace(scope: str, items: list[dict]) -> None:
    """Overwrite the queue with ``items``.

    Used by the ``PATCH``/``DELETE /chat/queue/{id}`` endpoints, which are
    read-modify-write by nature. A concurrent ``queue_push`` can interleave
    between the caller's snapshot and this replace; the window is bounded by the
    session lock held for the duration of a turn, and is no worse than the
    in-process version it replaces (which had the same window, plus no
    cross-process visibility at all).
    """
    if not enabled():
        _LOCAL_QUEUES[scope] = list(items)
        return
    try:
        client = _get_client()
        pipe = client.pipeline()
        pipe.delete(_queue_key(scope))
        for item in items:
            pipe.rpush(_queue_key(scope), json.dumps(item))
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        _LOCAL_QUEUES[scope] = list(items)


# --------------------------------------------------------------------------- #
# cancel flag
# --------------------------------------------------------------------------- #
def set_cancel(scope: str) -> None:
    """Flag this session as cancelled (visible to every process)."""
    if not enabled():
        _LOCAL_CANCELS[scope] = True
        return
    try:
        _get_client().setex(_cancel_key(scope), CANCEL_TTL_SECS, "1")
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        _LOCAL_CANCELS[scope] = True


def is_cancelled(scope: str) -> bool:
    """True when a cancel has been requested for this session."""
    if not enabled():
        return bool(_LOCAL_CANCELS.get(scope))
    try:
        return bool(_get_client().exists(_cancel_key(scope)))
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        return bool(_LOCAL_CANCELS.get(scope))


def clear_cancel(scope: str) -> None:
    """Drop the cancel flag.

    Called at the start of a fresh turn (so a stale flag from a previous turn
    cannot abort it) and in the stream's ``finally``.
    """
    if not enabled():
        _LOCAL_CANCELS.pop(scope, None)
        return
    try:
        _get_client().delete(_cancel_key(scope))
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        _LOCAL_CANCELS.pop(scope, None)


# --------------------------------------------------------------------------- #
# live progress
# --------------------------------------------------------------------------- #
#: Live progress is ephemeral: a record that outlives its turn is a lie.
PROGRESS_TTL_SECS = 7200

#: Fallback store, used when sharing is off or Redis is unavailable.
_LOCAL_PROGRESS: dict[str, dict] = {}


def _progress_key(scope: str) -> str:
    return f"progress:{scope}"


def progress_set(scope: str, record: dict) -> None:
    """Publish the whole progress record for ``scope``.

    One key, one JSON blob, one round-trip. The record is a few hundred bytes and
    is written once per tool round / per tool boundary -- NOT per token -- so the
    cost is bounded by round count, not response size.
    """
    if not enabled():
        _LOCAL_PROGRESS[scope] = dict(record)
        return
    try:
        _get_client().setex(_progress_key(scope), PROGRESS_TTL_SECS, json.dumps(record))
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        _LOCAL_PROGRESS[scope] = dict(record)


def progress_get(scope: str) -> dict | None:
    """Read the progress record for ``scope``, or ``None`` when idle."""
    if not enabled():
        rec = _LOCAL_PROGRESS.get(scope)
        return dict(rec) if rec else None
    try:
        raw = _get_client().get(_progress_key(scope))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        rec = _LOCAL_PROGRESS.get(scope)
        return dict(rec) if rec else None


def progress_clear(scope: str) -> None:
    """Remove the progress record (turn ended, success or error)."""
    if not enabled():
        _LOCAL_PROGRESS.pop(scope, None)
        return
    try:
        _get_client().delete(_progress_key(scope))
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
    _LOCAL_PROGRESS.pop(scope, None)


def progress_scopes() -> list[str]:
    """All scopes currently holding a progress record.

    Used by the manual-compaction tool to refuse touching a session that is
    mid-turn in another thread. Local-only when sharing is disabled.
    """
    scopes = set(_LOCAL_PROGRESS)
    if enabled():
        try:
            client = _get_client()
            for key in client.scan_iter(match="progress:*", count=200):
                scopes.add(str(key).split(":", 1)[1])
        except Exception as exc:  # noqa: BLE001
            _warn(exc)
    return sorted(scopes)
