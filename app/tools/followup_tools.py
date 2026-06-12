"""
Follow-up tools for the durable follow-up monitor.

Tools:
- add_followup: create a new tracked follow-up (REQUIRES thread_id)
- list_followups: list open follow-ups
- close_followup: resolve or abort a follow-up

"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.followups import (
    _FOLLOWUPS_MD,
    _read_md,
    _write_md,
    get_state,
    list_open,
    parse_all,
    set_status,
    upsert_state,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_thread_id(ctx: Any) -> str | None:
    """Extract thread_id from session context if available."""
    session_id = getattr(ctx, "session_id", None) or ""
    # Format: tg:<chat_id>:<thread_id>
    parts = session_id.split(":")
    if len(parts) >= 3 and parts[0] == "tg":
        return parts[2]
    return None


def _derive_chat_id(ctx: Any) -> str | None:
    """Extract chat_id from session context if available."""
    session_id = getattr(ctx, "session_id", None) or ""
    parts = session_id.split(":")
    if len(parts) >= 2 and parts[0] == "tg":
        return parts[1]
    return None


def _is_telegram_session(ctx: Any) -> bool:
    """Check if the current session is a Telegram session."""
    session_id = getattr(ctx, "session_id", None) or ""
    return session_id.startswith("tg:")


def _build_followup_block(followup: dict[str, Any]) -> str:
    """Build a ```followup fenced block from a dict."""
    lines = ["```followup"]
    for key, value in followup.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (int, float)):
                    lines.append(f"  {sub_key}: {sub_value}")
                else:
                    lines.append(f"  {sub_key}: {sub_value}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("```")
    return "\n".join(lines)


# ── tools ─────────────────────────────────────────────────────────────────


def add_followup(
    ctx: Any,
    id: str,
    title: str,
    condition_kind: str = "elapsed_days",
    escalate_after_days: int = 1,
    check: str = "daily",
    thread_id: str | None = None,
    chat_id: str | None = None,
    description: str = "",
    **extra_condition_kwargs: Any,
) -> str:
    """
    Create a new tracked follow-up.

    REQUIRES thread_id (or a Telegram session context to derive it from).
    Refuses non-Telegram sessions.

    Args:
        id: Unique slug for this follow-up (e.g. 'matheus-nota-fiscal')
        title: Human-readable title
        condition_kind: 'elapsed_days' or 'gmail_reply'
        escalate_after_days: Days after which to ping the thread
        check: 'daily' or 'weekly'
        thread_id: Telegram thread ID (derived from session if omitted)
        chat_id: Telegram chat ID (derived from session if omitted)
        description: Optional longer description
        **extra_condition_kwargs: Extra condition fields (e.g. from=, subject_contains=)
    """
    # Refuse non-Telegram sessions
    if not _is_telegram_session(ctx):
        return json.dumps({
            "status": "error",
            "message": "add_followup requires a Telegram session. "
                       "Cannot create a thread-less silent follow-up.",
        })

    # Derive thread_id from session if not provided
    if not thread_id:
        thread_id = _derive_thread_id(ctx)
    if not thread_id:
        return json.dumps({
            "status": "error",
            "message": "thread_id is required. Cannot create a follow-up "
                       "without a thread to report back to.",
        })

    # Derive chat_id from session if not provided
    if not chat_id:
        chat_id = _derive_chat_id(ctx)
    if not chat_id:
        chat_id = "-1003919341801"  # fallback to working group

    # Build the follow-up dict
    now = _now_iso()
    condition: dict[str, Any] = {"kind": condition_kind}
    if condition_kind == "gmail_reply":
        if "from" in extra_condition_kwargs:
            condition["from"] = extra_condition_kwargs["from"]
        if "subject_contains" in extra_condition_kwargs:
            condition["subject_contains"] = extra_condition_kwargs["subject_contains"]
    elif condition_kind == "elapsed_days":
        pass

    followup = {
        "id": id,
        "chat_id": chat_id,
        "thread_id": int(thread_id) if thread_id.isdigit() else thread_id,
        "title": title,
        "created_at": now[:10],  # YYYY-MM-DD
        "condition": condition,
        "schedule": {
            "check": check,
            "escalate_after_days": escalate_after_days,
            "on_escalate": "ping_thread",
        },
        "status": "open",
    }
    if description:
        followup["description"] = description

    # Build the fenced block
    block = _build_followup_block(followup)

    # Append to OPEN_FOLLOWUPS.md under ## Pending
    content = _read_md()
    pending_marker = "\n## Pending\n"
    pending_idx = content.find(pending_marker)
    if pending_idx >= 0:
        # Insert after the Pending section header
        insert_point = pending_idx + len(pending_marker)
        new_content = content[:insert_point] + "\n" + block + "\n\n" + content[insert_point:]
    else:
        # No Pending section — append at end
        new_content = content.rstrip() + "\n\n## Pending\n\n" + block + "\n"

    _write_md(new_content)

    # Seed sidecar state
    upsert_state(id, next_check=now, last_checked=None, attempts=0)

    return json.dumps({
        "status": "ok",
        "message": f"Follow-up '{id}' created and tracked.",
        "followup": {
            "id": id,
            "title": title,
            "thread_id": thread_id,
            "condition": condition_kind,
            "escalate_after_days": escalate_after_days,
        },
    })


def list_followups(ctx: Any, this_thread: bool = False) -> str:
    """
    List open follow-ups.

    Args:
        this_thread: If True, only show follow-ups for the current thread.
    """
    open_fups = list_open()

    if not open_fups:
        return json.dumps({
            "status": "ok",
            "message": "No open follow-ups.",
            "followups": [],
        })

    current_thread = _derive_thread_id(ctx)
    current_chat = _derive_chat_id(ctx)

    if this_thread and current_thread:
        open_fups = [
            f for f in open_fups
            if str(f.get("thread_id", "")) == current_thread
            and str(f.get("chat_id", "")) == (current_chat or "")
        ]

    if not open_fups:
        return json.dumps({
            "status": "ok",
            "message": "No open follow-ups in this thread." if this_thread else "No open follow-ups.",
            "followups": [],
        })

    # Enrich with sidecar state
    enriched = []
    for f in open_fups:
        state = get_state(f["id"]) or {}
        entry = {
            "id": f["id"],
            "title": f["title"],
            "thread_id": f.get("thread_id"),
            "condition": f.get("condition", {}).get("kind", "unknown"),
            "escalate_after_days": f.get("schedule", {}).get("escalate_after_days", 1),
            "created_at": f.get("created_at", ""),
            "attempts": state.get("attempts", 0),
            "last_checked": state.get("last_checked"),
            "next_check": state.get("next_check"),
            "this_thread": (
                str(f.get("thread_id", "")) == current_thread
                and str(f.get("chat_id", "")) == (current_chat or "")
            ) if current_thread else False,
        }
        enriched.append(entry)

    return json.dumps({
        "status": "ok",
        "count": len(enriched),
        "followups": enriched,
    })


def close_followup(ctx: Any, id: str, status: str = "resolved") -> str:
    """
    Close a follow-up (resolve or abort).

    Args:
        id: The follow-up id to close.
        status: 'resolved' or 'aborted'.
    """
    if status not in ("resolved", "aborted"):
        return json.dumps({
            "status": "error",
            "message": f"Invalid status '{status}'. Use 'resolved' or 'aborted'.",
        })

    result = set_status(id, status)
    if not result:
        return json.dumps({
            "status": "error",
            "message": f"Follow-up '{id}' not found.",
        })

    return json.dumps({
        "status": "ok",
        "message": f"Follow-up '{id}' marked as {status}.",
    })
