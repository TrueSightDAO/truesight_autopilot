"""Active-supervision claims -- Sophia's side of ``handoffs/active_supervision.json``.

PR4c(a): make Sophia's own claim a *side-effect* of a turn rather than a manual step.
The shared claims file is read by ``scripts/build_handoff_index.py`` to paint the
board's "Envoy supervised" lane. A turn on a handoff thread claims/refreshes Sophia's
entry at turn start and releases it on clean turn completion.

Single writer: this module is the only writer of Sophia-originated claims; Envoy
writes its own entries via a docs PR. Writes go through ``GitHubClient`` against
``agentic_ai_context`` ``main`` so a claim is visible to the board immediately and
never races the ~5-min local context sync.

Fail-soft by construction: every public function swallows its errors (logged at
debug) -- a claim is a visibility nicety and must NEVER block or fail a governor's
turn. Mirrors ``app/resume_registry.py``'s contract.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("autopilot.supervision")

_REPO = "agentic_ai_context"
_PATH = "handoffs/active_supervision.json"
SCHEMA_VERSION = 1

# The supervisor string Sophia writes for her own claims. Distinct from Envoy's
# "Envoy (nelanco-claude, supervisor loop)" so the board can tell the two apart.
SELF_SUPERVISOR = "Sophia (autopilot, self)"

# Serialize read-modify-write of the claims file within this process so two
# threads claiming on the same tick can't clobber each other's entry.
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_plan(plan: str) -> str:
    """Match ``build_handoff_index._norm_plan_key``: strip backticks + whitespace."""
    return (plan or "").strip().strip("`").strip()


def _read_doc() -> dict:
    """Fetch the claims file. Returns a minimal skeleton on any failure."""
    skeleton: dict = {"schema_version": SCHEMA_VERSION, "claims": []}
    try:
        from .github_client import GitHubClient

        res = GitHubClient().read_file(_REPO, _PATH)
        if res.get("type") != "file":
            return skeleton
        doc = json.loads(res["content"])
        if not isinstance(doc, dict) or not isinstance(doc.get("claims"), list):
            return skeleton
        return doc
    except Exception as e:  # never block a turn
        logger.debug("supervision read failed: %s", e)
        return skeleton


def _write_doc(doc: dict, message: str) -> bool:
    try:
        from .github_client import GitHubClient

        body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        return bool(GitHubClient().commit_file(_REPO, "main", _PATH, body, message))
    except Exception as e:  # never block a turn
        logger.debug("supervision write failed: %s", e)
        return False


def _new_entry(plan: str, supervisor: str, note: str) -> dict:
    return {
        "plan_file": plan,
        "supervisor": supervisor,
        "claimed_at": _now(),
        "note": note or "",
    }


def claim(plan_file: str, *, note: str = "", supervisor: str = SELF_SUPERVISOR) -> bool:
    """Upsert a claim for ``plan_file`` (refresh ``claimed_at`` if already ours)."""
    plan = normalize_plan(plan_file)
    if not plan:
        return False
    try:
        with _lock:
            doc = _read_doc()
            claims = doc["claims"]
            found = False
            for c in claims:
                if (
                    isinstance(c, dict)
                    and normalize_plan(c.get("plan_file", "")) == plan
                    and c.get("supervisor") == supervisor
                ):
                    c["claimed_at"] = _now()
                    if note:
                        c["note"] = note
                    found = True
            if not found:
                claims.append(_new_entry(plan, supervisor, note))
            doc["schema_version"] = doc.get("schema_version", SCHEMA_VERSION)
            return _write_doc(doc, f"handoffs: claim {plan} ({supervisor})")
    except Exception as e:  # never block a turn
        logger.debug("supervision claim failed: %s", e)
        return False


def release(plan_file: str, *, supervisor: str = SELF_SUPERVISOR) -> bool:
    """Remove ``supervisor``'s claim for ``plan_file``. No-op (False) if absent."""
    plan = normalize_plan(plan_file)
    if not plan:
        return False
    try:
        with _lock:
            doc = _read_doc()
            claims = doc["claims"]
            kept = [
                c
                for c in claims
                if not (
                    isinstance(c, dict)
                    and normalize_plan(c.get("plan_file", "")) == plan
                    and c.get("supervisor") == supervisor
                )
            ]
            if len(kept) == len(claims):
                return False
            doc["claims"] = kept
            return _write_doc(doc, f"handoffs: release {plan} ({supervisor})")
    except Exception as e:  # never block a turn
        logger.debug("supervision release failed: %s", e)
        return False


def _plan_for_thread(thread_id) -> str | None:
    """Resolve a Telegram forum-topic id -> active handoff plan file (or None)."""
    try:
        from .telegram_adapter import _handoff_plan_for_thread

        return _handoff_plan_for_thread(thread_id)
    except Exception as e:  # never block a turn
        logger.debug("supervision thread->plan resolve failed: %s", e)
        return None


def claim_for_thread(
    thread_id, *, note: str = "auto-claim (turn-dispatch)"
) -> str | None:
    """Turn-start hook: claim the thread's handoff plan. Returns the plan or None."""
    plan = _plan_for_thread(thread_id)
    if not plan:
        return None
    return plan if claim(plan, note=note) else None


def claim_for_turn(thread_id, plan_file: str | None = None) -> str | None:
    """Resolve + claim the plan for one turn, returning the claimed plan or None.

    An explicit ``plan_file`` (named by ``ping_sophia`` when Envoy hands a thread
    off) wins over inferring the plan from the session's thread id. Fail-soft:
    returns None rather than raising, so a claim can never block a turn.
    """
    try:
        plan = normalize_plan(plan_file) if plan_file else ""
        if plan:
            return plan if claim(plan, note="auto-claim (ping_sophia)") else None
        return claim_for_thread(thread_id)
    except Exception as e:  # never block a turn
        logger.debug("claim_for_turn failed: %s", e)
        return None
