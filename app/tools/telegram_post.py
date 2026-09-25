"""Post a message into an **existing** Telegram forum topic (by thread_id).

Companion to ``create_telegram_topic``. The Bot API (and the adapter's
``send_message``) already support ``message_thread_id`` — this exposes that as a
tool so Sophia can:

  * **hand off / post updates into an existing thread** (e.g. tell the chocolate
    subscription thread "sandbox is ready") without spawning a new topic, and
  * **rejoin** a handoff thread a ping references, rather than creating a churned
    duplicate (the 1924→1939 problem).

chat_id resolution mirrors ``create_telegram_topic``: explicit ``chat_id`` →
current ``tg:`` session's chat → ``settings.telegram_home_group_id``.
"""

from __future__ import annotations

import json
import logging
import secrets

import httpx

from ..config import settings
from .. import resume_registry
from ..tool_registry import ToolSpec
from .telegram_topic import _API, _TIMEOUT, _chat_id_from_session, _deep_link

logger = logging.getLogger("autopilot.tools.telegram_post")

# Token alphabet for resume-option refs -- excludes 0/O/1/I so a governor can
# retype a ref (e.g. "K7QM-2") unambiguously when they reply by text instead
# of tapping a button.
_TOKEN_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _new_option_token() -> str:
    """A short, unambiguous ref token not currently live in the registry."""
    for _ in range(8):
        tok = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(4))
        if not resume_registry.peek_options(tok):
            return tok
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(6))


def _build_options_keyboard(token: str, options: list[str]) -> dict:
    """One button per option (numbered), plus a non-decision 'Other' button.

    callback_data is ``ro:<token>:<index>`` -- kept well under Telegram's
    64-byte cap because the *labels* live server-side in the registry.
    """
    rows: list[list[dict]] = []
    for i, opt in enumerate(options):
        label = opt if len(opt) <= 56 else opt[:55] + "\u2026"
        rows.append([{"text": f"{i + 1}. {label}", "callback_data": f"ro:{token}:{i}"}])
    rows.append(
        [
            {
                "text": "\u270d\ufe0f Other (type a message)",
                "callback_data": f"ro:{token}:o",
            }
        ]
    )
    return {"inline_keyboard": rows}


def post_to_telegram_topic(
    message: str,
    thread_id: int | str,
    chat_id: str | None = None,
    session_id: str | None = None,
    resume_awaiting: bool = False,
    options: list[str] | None = None,
) -> dict:
    message = (message or "").strip()
    if not message:
        return {"status": "error", "reason": "message is required"}
    try:
        thread = int(thread_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "reason": "thread_id (the existing topic's message_thread_id) is required and must be numeric",
        }

    token = settings.telegram_bot_api_key
    if not token:
        return {
            "status": "error",
            "reason": "TELEGRAM_BOT_API_KEY not configured on this box",
        }

    target = (
        chat_id
        or _chat_id_from_session(session_id)
        or (
            str(settings.telegram_home_group_id)
            if settings.telegram_home_group_id
            else None
        )
    )
    if not target:
        return {
            "status": "error",
            "reason": "no target group — not in a Telegram topic session and "
            "TELEGRAM_HOME_GROUP_ID is unset. Pass chat_id.",
        }

    # Optional multiple-choice menu: numbered inline buttons + a ref code the
    # governor can retype. Labels are stored server-side (Telegram caps
    # callback_data at 64 bytes), keyed by an opaque token.
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    opt_token = _new_option_token() if opts else ""
    if opts:
        message = (
            f"{message}\n\n\u21a9\ufe0f Reply [{opt_token}-1]\u2026[{opt_token}-"
            f"{len(opts)}] \u2014 or tap a button below."
        )
    payload: dict = {
        "chat_id": target,
        "message_thread_id": thread,
        "text": message,
    }
    if opts:
        payload["reply_markup"] = _build_options_keyboard(opt_token, opts)
    try:
        r = httpx.post(
            f"{_API}/bot{token}/sendMessage",
            json=payload,
            timeout=_TIMEOUT,
        )
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"sendMessage call failed: {e}"}

    if not data.get("ok"):
        desc = data.get("description", "unknown error")
        hint = (
            "Verify the thread_id is a real topic in this group and Sophia's "
            "bot can post there (group admin / not restricted)."
        )
        return {
            "status": "error",
            "reason": f"Telegram: {desc}",
            "hint": hint,
            "chat_id": target,
            "message_thread_id": thread,
        }

    # Unconditional (dropped the resume_awaiting/looks_resume_awaiting regex
    # gate, 2026-08-29): any positive-emoji reaction on any posted message
    # means "continue", not just specially-flagged ones.
    if data.get("ok"):
        mid = (data.get("result") or {}).get("message_id")
        if opts:
            resume_registry.mark_options(opt_token, thread, opts)
        if mid:
            resume_registry.mark_resume_awaiting(mid, thread, message)

    logger.info("posted to existing topic (thread=%s) in chat %s", thread, target)
    return {
        "status": "ok",
        "message_thread_id": thread,
        "chat_id": target,
        "message_id": (data.get("result") or {}).get("message_id"),
        "option_token": opt_token or None,
        "options": opts or None,
        "link": _deep_link(target, thread),
    }


TOOL_SPEC = ToolSpec(
    name="post_to_telegram_topic",
    description=(
        "Post a message into an EXISTING Telegram forum topic, identified by its "
        "message_thread_id. Use this to hand off or report into a thread that "
        "already exists (e.g. tell a parked handoff thread that a dependency is "
        "ready, or rejoin a thread a ping referenced) — do NOT create a new topic "
        "with create_telegram_topic for that. The target group defaults to the "
        "current group / configured working group."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to post into the topic.",
            },
            "thread_id": {
                "type": "integer",
                "description": "The existing topic's message_thread_id (e.g. 1955).",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional explicit group chat id; defaults to current/working group.",
            },
            "resume_awaiting": {
                "type": "boolean",
                "description": "If true, flag the posted message as resume-awaiting so a governor's emoji reaction on it can act as a go-signal.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional multiple-choice menu: renders one tap-button per option plus a non-decision 'Other' button, so the governor can answer a branching resume with one tap instead of typing. Use for GENUINELY mutually-exclusive choices; still include the recommended option so a plain 'go' works.",
            },
        },
        "required": ["message", "thread_id"],
    },
    handler=lambda args, ctx: json.dumps(
        post_to_telegram_topic(
            message=args.get("message", ""),
            thread_id=args.get("thread_id"),
            chat_id=args.get("chat_id"),
            session_id=ctx.get("session_id"),
            resume_awaiting=bool(args.get("resume_awaiting", False)),
            options=list(args.get("options") or []),
        ),
        indent=2,
    ),
    default_roles=None,  # uniform — same as create_telegram_topic
)
