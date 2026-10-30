"""Read-only live-state snapshot for the public sprint board.

Why this module exists
----------------------
``SOPHIA_LIVE_PROGRESS_PLAN.md`` built live progress introspection, and it is
COMPLETE -- ``session_state.progress_scopes()`` already enumerates every
in-flight session. But the only readers are ``/chat/progress`` and
``/chat/queue``, and both are **caller-scoped**: they answer "what is *my*
session doing", never "what is the fleet doing".

The sprint board (``sprint.truesight.me``) needs the fleet view. This module is
the whole of that bridge: one function, no state, no writes.

Security posture
----------------
This surface is **PUBLIC**. The progress record written by ``main.py`` carries
``instruction`` (the user's prompt) and ``current_arg`` (free-form tool
arguments, which routinely contain QR codes, emails, filesystem paths and
names). Neither may ever leave the box.

So exposure is an **allowlist** (``PUBLIC_FIELDS``), not a denylist. A denylist
fails open: the day someone adds a field to the writer, it silently becomes
public. An allowlist fails closed -- a new field stays private until a human
adds it here deliberately. ``tests/test_live_state.py`` asserts this property
directly, including for keys the writer does not yet emit.

Degradation
-----------
Every source is best-effort. Redis down, claims file unreadable, bad JSON --
all yield an empty section and a ``degraded`` note, never a raised exception. A
monitoring endpoint that 500s when the thing it monitors is unhealthy is worse
than useless.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import dispatch_registry, session_state

logger = logging.getLogger(__name__)

#: The complete set of progress fields allowed to leave the process.
#:
#: Deliberately excludes ``instruction`` and ``current_arg``. ``done`` is
#: included but reduced to step names by :func:`_public_steps`.
PUBLIC_FIELDS = (
    "current_tool",
    "round",
    "elapsed",
    "started_at",
    "queue_depth",
    "done",
)

#: Fields that must never appear in the payload. Asserted by tests so a future
#: refactor cannot reintroduce them by accident.
FORBIDDEN_FIELDS = ("instruction", "current_arg", "session_id", "public_key")

_REPO = "TrueSightDAO/agentic_ai_context"
_CLAIMS_PATH = "handoffs/active_supervision.json"

#: Cap the number of sessions returned. A runaway loop could otherwise produce
#: an unbounded public response; the board only ever shows a handful.
MAX_SESSIONS = 25

#: Hard bound on how many scopes we will *scan*. Sessions are sorted before
#: being truncated to :data:`MAX_SESSIONS`, so this must exceed it -- otherwise
#: "newest first" would really mean "first N in arbitrary scope order".
MAX_SCAN = 200


def enabled() -> bool:
    """True when the public live-state surface is switched on.

    Mirrors ``session_state.enabled()`` so the flag convention stays uniform.
    """
    return os.environ.get("LIVE_STATE_PUBLIC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _public_steps(done: Any) -> list[str]:
    """Reduce the ``done`` list to short step labels only.

    The writer may store richer structures; a *public* field must not forward
    whatever happens to be there. Accepts a list of strings or dicts carrying a
    ``tool``/``name`` key and returns plain strings.
    """
    if not isinstance(done, list):
        return []
    out: list[str] = []
    for item in done[:20]:  # bound the size of a public payload
        if isinstance(item, str):
            out.append(item[:80])
        elif isinstance(item, dict):
            label = item.get("tool") or item.get("name") or item.get("step")
            if isinstance(label, str):
                out.append(label[:80])
    return out


def _public_session(record: Any) -> dict[str, Any] | None:
    """Project one progress ``record`` onto :data:`PUBLIC_FIELDS`.

    Returns ``None`` when there is nothing worth showing. Iterating
    ``PUBLIC_FIELDS`` (rather than the record) is the whole point: an unknown
    key in ``record`` cannot reach the output.
    """
    if not isinstance(record, dict) or not record:
        return None

    out: dict[str, Any] = {}
    for field in PUBLIC_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if field == "done":
            steps = _public_steps(value)
            if steps:
                out["done"] = steps
            continue
        if field in ("current_tool", "instruction"):
            out[field] = str(value)[:80] if value is not None else None
            continue
        out[field] = value

    # A session that has done nothing and is on no tool is noise, not signal.
    if not out.get("current_tool") and not out.get("done"):
        return None
    return out


def _sessions() -> tuple[list[dict[str, Any]], str | None]:
    """Collect every in-flight session, newest first.

    Returns ``(sessions, note)`` where ``note`` describes any degradation.
    """
    try:
        scopes = session_state.progress_scopes()
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.debug("progress_scopes failed: %s", exc)
        return [], "progress store unavailable"

    sessions: list[dict[str, Any]] = []
    for scope in list(scopes)[:MAX_SCAN]:
        try:
            record = session_state.progress_get(scope)
        except Exception as exc:  # pragma: no cover
            logger.debug("progress_get(%s) failed: %s", scope, exc)
            continue
        projected = _public_session(record)
        if projected is not None:
            sessions.append(projected)

    # Most recently started first; missing/unparseable start times sort last.
    sessions.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
    # Truncate AFTER sorting: the board wants the newest N, not an arbitrary N.
    sessions = sessions[:MAX_SESSIONS]

    # An empty board is not a fault. Reporting it through ``degraded`` made the
    # public board show "Partially degraded -- some sections could not be read:
    # no in-flight sessions" whenever no turn happened to be mid-flight, which
    # is most of the time. It also buried genuine read failures in the same
    # list, so a real fault looked identical to a quiet moment. The empty case
    # is now surfaced separately as the informational ``sessions_note``.
    return sessions, None


def _active_claims() -> tuple[list[dict[str, Any]], str | None]:
    """Read ``handoffs/active_supervision.json`` -- best effort.

    Read via the Contents API rather than a local clone: the autopilot box is
    not guaranteed to have the context repo checked out, and a monitoring
    endpoint must not depend on filesystem layout.
    """
    try:
        from .github_client import GitHubClient

        res = GitHubClient().read_file(_REPO, _CLAIMS_PATH)
        if res.get("type") != "file":
            return [], "claims file missing"
        doc = json.loads(res["content"])
    except Exception as exc:
        logger.debug("active claims read failed: %s", exc)
        return [], "claims file unavailable"

    claims = doc.get("claims") if isinstance(doc, dict) else None
    if not isinstance(claims, list):
        return [], "claims file malformed"

    out: list[dict[str, Any]] = []
    for entry in claims:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "plan_file": entry.get("plan_file"),
                "supervisor": entry.get("supervisor"),
                "claimed_at": entry.get("claimed_at"),
                "note": (entry.get("note") or "")[:160],
            }
        )
    out.sort(key=lambda c: c.get("claimed_at") or "", reverse=True)
    return out, None


def _workers() -> tuple[dict[str, Any], str | None]:
    """Fleet view from ``dispatch_registry`` -- best effort.

    Returns ``(snapshot, note)``. The snapshot is already a public-safe
    projection (``worker_ref``, never ``host:pid``), so this only guards against
    a broken registry: the board must degrade, never 500.
    """
    try:
        snap = dispatch_registry.snapshot()
    except Exception as exc:  # noqa: BLE001 -- registry never raises
        logger.debug("dispatch_registry.snapshot failed: %s", exc)
        return (
            {"enabled": False, "workers": [], "worker_count": 0},
            "worker registry unavailable",
        )
    note = None
    if snap.get("degraded"):
        note = snap.get("reason") or "worker registry unavailable"
    return snap, note


def live_state_payload() -> dict[str, Any]:
    """Build the complete public snapshot. Never raises."""
    sessions, sessions_note = _sessions()
    claims, claims_note = _active_claims()

    workers, workers_note = _workers()

    degraded = [n for n in (sessions_note, claims_note, workers_note) if n]

    return {
        "enabled": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generated_at_epoch": time.time(),
        "shared_state": session_state.enabled(),
        "sessions": sessions,
        "session_count": len(sessions),
        # Informational, NOT a fault: distinguishes "nothing is running right
        # now" from "we could not read the state". The board renders an empty
        # lane as plain text (its own "nothing is in flight" copy); only
        # ``degraded`` earns the warning banner.
        "sessions_note": None if sessions else "no in-flight sessions",
        "active_claims": claims,
        "workers": workers,
        "claim_count": len(claims),
        "degraded": degraded,
    }
