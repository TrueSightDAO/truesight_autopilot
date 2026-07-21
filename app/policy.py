"""Policy layer — identity resolver + authorization gate.

Resolves (tenant, surface, identity, action) → allow/deny.

Identity resolver:
  telegram_id → env allowlist, OR Column X (Contributors contact
  information) → Governors cache → {guest, governor}

A telegram_id verified through the Telegram /verify flow is written to
Column X; if that contributor is in the Governors cache, they resolve to
GOVERNOR even on a device whose id was never added to the env allowlist.

The full (tenant × surface × identity × action) key is stubbed for now —
tenant defaults to "truesight", surface is inferred from context, and
action-class is resolved by the caller. Phase 0.2–0.4 will add tool-layer
enforcement and the data/instruction boundary.

Security invariant #1: enforce at the tool layer, never the prompt.
This module is the tool layer's gate — it is called BEFORE any write/admin
tool executes, not as a prompt instruction.
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Identity & Role types ────────────────────────────────────────────────


class Role(enum.Enum):
    """Resolved role for a requester."""

    GUEST = "guest"
    GOVERNOR = "governor"


class ActionClass(enum.Enum):
    """Classification of an action for policy evaluation."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    SECRET = "secret"  # never returned in chat; vault page only


@dataclass(frozen=True)
class Identity:
    """Resolved identity of a requester."""

    telegram_id: int | None
    role: Role
    name: str | None = None
    # Future: tenant, surface, public_key, email, etc.


# ── Governor registry (v0: env-var based) ────────────────────────────────
# Phase 1 will replace this with the Column X → Governors cache lookup.
# For now, the GOVERNOR_NAMES env var (comma-separated display names) and
# the TELEGRAM_ALLOWED_USER_IDS env var (comma-separated numeric IDs) are
# the source of truth.

_GOVERNOR_NAMES: set[str] | None = None
_GOVERNOR_TELEGRAM_IDS: set[int] | None = None

# Cache for the Column X → binding lookup (telegram_id → {email, name} or None).
# Bindings change rarely; a short TTL keeps the per-message hot path off the
# Sheets API. Lookup FAILURES are never cached, so they retry.
_BINDING_CACHE_TTL = int(os.getenv("BINDING_CACHE_TTL", "300"))
_binding_cache: dict[int, tuple[float, dict | None]] = {}


def _load_governor_names() -> set[str]:
    global _GOVERNOR_NAMES
    if _GOVERNOR_NAMES is None:
        raw = os.getenv("GOVERNOR_NAMES", "Gary Teh")
        _GOVERNOR_NAMES = {name.strip() for name in raw.split(",") if name.strip()}
    return _GOVERNOR_NAMES


def _load_governor_telegram_ids() -> set[int]:
    global _GOVERNOR_TELEGRAM_IDS
    if _GOVERNOR_TELEGRAM_IDS is None:
        raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part:
                try:
                    ids.add(int(part))
                except ValueError:
                    logger.warning(
                        "Non-numeric TELEGRAM_ALLOWED_USER_IDS entry: %r", part
                    )
        _GOVERNOR_TELEGRAM_IDS = ids
    return _GOVERNOR_TELEGRAM_IDS


def refresh_governor_cache() -> None:
    """Force-reload governor config from env (e.g. after a deploy).

    Only nulls the caches — does NOT pre-load, so the next call to
    _load_governor_names() / _load_governor_telegram_ids() picks up
    whatever env is current at that point (important for test isolation
    with patch.dict).
    """
    global _GOVERNOR_NAMES, _GOVERNOR_TELEGRAM_IDS
    _GOVERNOR_NAMES = None
    _GOVERNOR_TELEGRAM_IDS = None
    _binding_cache.clear()


# ── Column X binding lookup (Phase 1 read-side) ──────────────────────────


def _resolve_binding(telegram_id: int) -> dict | None:
    """Look up the verified identity bound to this telegram_id (Column X).

    Returns ``{"email", "name"}`` if the id is bound to a verified
    contributor, else ``None``. Cached with a short TTL; lookup failures
    (network, missing creds) return None and are NOT cached so they retry.
    """
    now = time.time()
    cached = _binding_cache.get(telegram_id)
    if cached is not None and (now - cached[0]) < _BINDING_CACHE_TTL:
        return cached[1]

    try:
        from .identity_binding import check_binding_status

        status = check_binding_status(telegram_id)
    except Exception as exc:  # noqa: BLE001 — degrade to guest, don't crash
        logger.warning("Binding lookup failed for telegram_id=%s: %s", telegram_id, exc)
        return None

    result: dict | None = None
    if status.get("bound"):
        result = {
            "email": (status.get("email") or "").strip(),
            "name": (status.get("name") or "").strip(),
        }
    _binding_cache[telegram_id] = (now, result)
    return result


def _binding_is_governor(email: str, name: str) -> bool:
    """Check a bound identity (email/name) against the Governors cache."""
    try:
        from .governor_registry import load_governors

        data = load_governors()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Governor cache lookup failed: %s", exc)
        return False

    email_l = (email or "").strip().lower()
    name_l = (name or "").strip().lower()
    for g in data.get("governors", []):
        if email_l and (g.get("email") or "").strip().lower() == email_l:
            return True
        if name_l and (g.get("name") or "").strip().lower() == name_l:
            return True
    return False


# ── Identity resolver ────────────────────────────────────────────────────


def resolve_identity(
    telegram_id: int | None = None,
    telegram_username: str | None = None,
    display_name: str | None = None,
) -> Identity:
    """Resolve a Telegram user's identity to a role.

    Resolution order:
    1. If telegram_id is in the env governor allowlist → GOVERNOR
    2. If telegram_id is bound (Column X) to a contributor in the
       Governors cache → GOVERNOR (a verified non-governor member falls
       through to GUEST, but keeps their real name for audit)
    3. If display_name matches a known governor name → GOVERNOR (env bridge)
    4. Otherwise → GUEST

    Args:
        telegram_id: Numeric Telegram user ID (from message.from.id).
        telegram_username: @username (optional, for logging).
        display_name: Display name from Telegram (optional, for matching).

    Returns:
        An Identity dataclass with the resolved role.
    """
    governor_ids = _load_governor_telegram_ids()
    governor_names = _load_governor_names()

    # 1. Telegram ID match (strongest signal)
    if telegram_id is not None and telegram_id in governor_ids:
        return Identity(
            telegram_id=telegram_id,
            role=Role.GOVERNOR,
            name=display_name or telegram_username or str(telegram_id),
        )

    # 2. Bound identity via Column X → Governors cache (Phase 1 read-side).
    bound_name: str | None = None
    if telegram_id is not None:
        bound = _resolve_binding(telegram_id)
        if bound is not None:
            bound_name = bound["name"] or None
            if _binding_is_governor(bound["email"], bound["name"]):
                return Identity(
                    telegram_id=telegram_id,
                    role=Role.GOVERNOR,
                    name=bound_name or display_name or telegram_username,
                )

    # 3. Display name match (weaker — Telegram display names can be changed)
    if display_name and display_name.strip() in governor_names:
        return Identity(
            telegram_id=telegram_id,
            role=Role.GOVERNOR,
            name=display_name.strip(),
        )

    # 4. Guest default (prefer a verified binding name for audit clarity)
    return Identity(
        telegram_id=telegram_id,
        role=Role.GUEST,
        name=bound_name
        or display_name
        or telegram_username
        or (str(telegram_id) if telegram_id else None),
    )


# ── Authorization helpers ────────────────────────────────────────────────


def is_governor(identity: Identity) -> bool:
    """Check if a resolved identity has governor privileges."""
    return identity.role == Role.GOVERNOR


def require_governor(identity: Identity, action_description: str = "") -> None:
    """Raise PermissionError if the identity is not a governor.

    Call this at the top of any write/admin tool before performing the action.
    This is the tool-layer enforcement point (Security invariant #1).

    Args:
        identity: The resolved identity of the requester.
        action_description: Human-readable description for the error message.

    Raises:
        PermissionError: If the identity is a guest.
    """
    if not is_governor(identity):
        name = identity.name or "Unknown user"
        desc = f" for {action_description}" if action_description else ""
        msg = f"Access denied{desc}: {name} is not a governor."
        logger.warning("POLICY DENY: %s (telegram_id=%s)", msg, identity.telegram_id)
        raise PermissionError(msg)


def may_access_secret(identity: Identity) -> bool:
    """Check if an identity may access secret/credential values.

    Secrets are NEVER returned in chat/transcripts/logs — this gate is for
    the vault web page only (Phase 3.3). Even governors don't get secrets
    through the chat interface.

    Returns:
        True only for governors (vault page authentication adds additional
        email→RSA verification on top of this).
    """
    return is_governor(identity)


# ── Action classification ────────────────────────────────────────────────


def classify_action(tool_name: str) -> ActionClass:
    """Classify a tool name into an action class for policy evaluation.

    This is a simple heuristic based on tool naming conventions.
    Phase 0.2 will replace this with an explicit manifest.

    Args:
        tool_name: The name of the tool being invoked.

    Returns:
        The action class for the tool.
    """
    # Write/admin tools that modify state
    write_tools = {
        "aws_query",
        "submit_contribution",
        "open_fix_pr",
        "git_push_changes",
        "merge_pr",
        "mark_pr_ready_for_review",
        "upload_file_to_github",
        "upload_local_file_to_github",
        "deploy_autopilot",
        "gmail_send",
        "gmail_create_draft",
        "gmail_apply_label",
        "create_dao_submission",
        "create_telegram_topic",
        "post_to_telegram_topic",
        "ssh_run",
        "register_identity",
        "gas_deploy_project",
        "sync_beta_to_prod",
        "generate_pdf",
        "push_to_personal_repo",
    }

    # Secret/credential tools — anything whose chat-facing return value could contain a raw
    # secret. NOTE: push_to_personal_repo consumes a vault credential server-side but never
    # returns it, so it's WRITE (governor-gated), not SECRET (unconditionally denied below) —
    # don't move it here.
    secret_tools = {
        # Phase 3 will add vault access tools here
    }

    if tool_name in secret_tools:
        return ActionClass.SECRET
    if tool_name in write_tools:
        return ActionClass.WRITE
    return ActionClass.READ


# ── Policy evaluation ────────────────────────────────────────────────────


@dataclass
class PolicyDecision:
    """Result of a policy evaluation."""

    allowed: bool
    identity: Identity
    action_class: ActionClass
    reason: str = ""


def evaluate(
    identity: Identity,
    tool_name: str,
    *,
    surface: str | None = None,
    tenant: str | None = None,
) -> PolicyDecision:
    """Evaluate whether an identity may perform an action.

    This is the single policy entry point. It resolves the action class
    from the tool name, then checks the identity's role against the
    action class.

    Phase 0.2 will add full (tenant × surface × identity × action) resolution.
    For now:
    - READ actions: allowed for everyone (guest-default)
    - WRITE/ADMIN actions: governor only
    - SECRET actions: never through chat (vault page only)

    Args:
        identity: The resolved identity.
        tool_name: The tool being invoked.
        surface: Optional surface identifier (e.g. "telegram:group:123").
        tenant: Optional tenant identifier (default: "truesight").

    Returns:
        A PolicyDecision with the result.
    """
    action_class = classify_action(tool_name)

    if action_class == ActionClass.READ:
        return PolicyDecision(
            allowed=True,
            identity=identity,
            action_class=action_class,
            reason="Read actions are open to all.",
        )

    if action_class == ActionClass.SECRET:
        return PolicyDecision(
            allowed=False,
            identity=identity,
            action_class=action_class,
            reason="Secret values are never returned through chat. Use the vault web page.",
        )

    # WRITE / ADMIN: governor only
    if is_governor(identity):
        return PolicyDecision(
            allowed=True,
            identity=identity,
            action_class=action_class,
            reason=f"Governor {identity.name} is authorized.",
        )

    return PolicyDecision(
        allowed=False,
        identity=identity,
        action_class=action_class,
        reason=f"Write/admin actions require governor privileges. {identity.name} is a guest.",
    )
