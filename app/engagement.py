"""Engagement modes — Phase 2 of the governance plan.

Defines per-surface engagement modes:
- proactive: Sophia replies to every message (current behavior, personal workspace)
- addressed-only: Sophia ingests everything but only replies when addressed
  (@mention, leading vocative, reply to her message)

Also handles DM policy and audit/announce channel.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

ENGAGEMENT_MODE_FILE = "data/engagement_modes.json"

# Sophia's name variants for addressed-only detection
SOPHIA_NAMES = [
    "sophia",
    "sophia,",
    "sophia.",
    "sophia!",
    "sophia?",
    "@sophia",
    "@sophiabot",
]

# ── Data types ─────────────────────────────────────────────────────────────


class EngagementMode:
    """Per-surface engagement mode configuration."""

    PROACTIVE = "proactive"
    ADDRESSED_ONLY = "addressed-only"

    @staticmethod
    def is_valid(mode: str) -> bool:
        return mode in (EngagementMode.PROACTIVE, EngagementMode.ADDRESSED_ONLY)


# ── Configuration store ────────────────────────────────────────────────────


def _config_path() -> Path:
    path = Path(ENGAGEMENT_MODE_FILE)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / ENGAGEMENT_MODE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_config() -> dict[str, Any]:
    """Load engagement mode configuration."""
    path = _config_path()
    if not path.exists():
        return {"version": 1, "surfaces": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load engagement config: %s", e)
        return {"version": 1, "surfaces": {}}


def _save_config(config: dict[str, Any]) -> None:
    """Save engagement mode configuration atomically."""
    path = _config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.rename(path)


def get_engagement_mode(chat_id: int, thread_id: int | None = None) -> str:
    """Get the engagement mode for a surface.

    Surfaces are identified by (chat_id, thread_id).
    Default is 'proactive' (current behavior).
    """
    config = _load_config()
    surface_key = _surface_key(chat_id, thread_id)
    mode = config.get("surfaces", {}).get(surface_key, {}).get("mode")
    if mode and EngagementMode.is_valid(mode):
        return mode
    return EngagementMode.PROACTIVE


def set_engagement_mode(
    chat_id: int,
    mode: str,
    thread_id: int | None = None,
    set_by: str = "system",
) -> bool:
    """Set the engagement mode for a surface.

    Args:
        chat_id: Telegram chat ID.
        mode: 'proactive' or 'addressed-only'.
        thread_id: Optional forum topic ID.
        set_by: Who set it (for audit).

    Returns:
        True if successful, False if invalid mode.
    """
    if not EngagementMode.is_valid(mode):
        return False

    config = _load_config()
    surface_key = _surface_key(chat_id, thread_id)
    config.setdefault("surfaces", {})[surface_key] = {
        "mode": mode,
        "set_by": set_by,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }
    _save_config(config)
    logger.info(
        "Engagement mode set to '%s' for surface %s by %s", mode, surface_key, set_by
    )
    return True


def _surface_key(chat_id: int, thread_id: int | None = None) -> str:
    """Generate a unique key for a surface."""
    if thread_id:
        return f"chat:{chat_id}:thread:{thread_id}"
    return f"chat:{chat_id}"


# ── Addressed-only detection ───────────────────────────────────────────────


def is_addressed(text: str, bot_username: str | None = None) -> bool:
    """Check if a message is addressed to Sophia.

    Returns True if:
    - Message starts with a Sophia name variant
    - Message contains @Sophia or @bot_username
    - Message is a reply to one of Sophia's messages (checked separately)

    Args:
        text: The message text.
        bot_username: The bot's Telegram @username (optional).

    Returns:
        True if the message appears to be addressed to Sophia.
    """
    if not text:
        return False

    text_lower = text.strip().lower()

    # Check for @mention
    if bot_username and f"@{bot_username.lower()}" in text_lower:
        return True

    # Check for Sophia name at the start of the message
    for name in SOPHIA_NAMES:
        if text_lower.startswith(name):
            return True
        # Also check after common prefixes
        if text_lower.startswith(f"hey {name}") or text_lower.startswith(f"hi {name}"):
            return True

    # Check for Sophia name anywhere in the first 50 chars
    first_50 = text_lower[:50]
    for name in SOPHIA_NAMES:
        clean_name = (
            name.replace(",", "").replace(".", "").replace("!", "").replace("?", "")
        )
        if clean_name in first_50:
            return True

    return False


def is_reply_to_sophia(msg: dict[str, Any], sophia_bot_id: int | None = None) -> bool:
    """Check if a message is a reply to one of Sophia's messages.

    Args:
        msg: The Telegram message dict.
        sophia_bot_id: The bot's own Telegram user ID.

    Returns:
        True if the message replies to a message sent by Sophia.
    """
    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return False

    if sophia_bot_id is None:
        # Try to get from env
        sophia_bot_id = int(os.getenv("TELEGRAM_BOT_ID", "0"))

    if not sophia_bot_id:
        return False

    from_user = reply_to.get("from") or {}
    return from_user.get("id") == sophia_bot_id


# ── DM policy ──────────────────────────────────────────────────────────────


def is_dm(chat: dict[str, Any]) -> bool:
    """Check if a chat is a direct message (not a group)."""
    chat_type = chat.get("type", "")
    return chat_type == "private"


def is_dm_write_allowed(user_id: int, allowed: set[int]) -> bool:
    """Check if a user is allowed to send write commands in DM.

    In DMs, only known/verified users can interact. Unknown users
    are rate-limited and only the verification flow is allowed.
    """
    return user_id in allowed


# ── Audit/announce channel ─────────────────────────────────────────────────


def get_audit_channel_id() -> int | None:
    """Get the configured audit/announce channel ID."""
    channel_id = os.getenv("AUDIT_CHANNEL_ID")
    if channel_id:
        try:
            return int(channel_id)
        except ValueError:
            logger.warning("Invalid AUDIT_CHANNEL_ID: %s", channel_id)
    return None


def format_audit_message(
    action: str,
    actor: str,
    details: str,
    surface: str = "",
) -> str:
    """Format a privileged action for the audit channel.

    Args:
        action: The action performed (e.g. 'deploy', 'git_push', 'email_send').
        actor: Who performed it.
        details: Additional context.
        surface: The surface/thread where it happened.

    Returns:
        Formatted audit message.
    """
    parts = [f"🔍 **{action}**"]
    parts.append(f"👤 {actor}")
    if surface:
        parts.append(f"📍 {surface}")
    if details:
        parts.append(f"📝 {details}")
    return " · ".join(parts)
