"""Policy layer — identity resolver + authorization gate.

Resolves (tenant, surface, identity, action) → allow/deny.

Phase 0.1 (vault-first order): identity resolver only.
  telegram_id → {guest, governor}

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
from dataclasses import dataclass, field
from typing import Literal

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
                    logger.warning("Non-numeric TELEGRAM_ALLOWED_USER_IDS entry: %r", part)
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


# ── Identity resolver ────────────────────────────────────────────────────


def resolve_identity(
    telegram_id: int | None = None,
    telegram_username: str | None = None,
    display_name: str | None = None,
) -> Identity:
    """Resolve a Telegram user's identity to a role.

    Resolution order:
    1. If telegram_id is in the governor allowlist → GOVERNOR
    2. If display_name matches a known governor name → GOVERNOR
    3. Otherwise → GUEST

    Phase 1 will add: telegram_id → Column X (Contributors contact) →
    Governors cache → {guest, governor}. The env-var approach is the
    v0 bridge until that plumbing exists.

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

    # 2. Display name match (weaker — Telegram display names can be changed)
    if display_name and display_name.strip() in governor_names:
        return Identity(
            telegram_id=telegram_id,
            role=Role.GOVERNOR,
            name=display_name.strip(),
        )

    # 3. Guest default
    return Identity(
        telegram_id=telegram_id,
        role=Role.GUEST,
        name=display_name or telegram_username or str(telegram_id) if telegram_id else None,
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
    }

    # Secret/credential tools
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
