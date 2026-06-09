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

import httpx

from ..config import settings
from ..tool_registry import ToolSpec
from .telegram_topic import _API, _TIMEOUT, _chat_id_from_session, _deep_link

logger = logging.getLogger("autopilot.tools.telegram_post")


def post_to_telegram_topic(message: str, thread_id: int | str,
                           chat_id: str | None = None,
                           session_id: str | None = None) -> dict:
    message = (message or "").strip()
    if not message:
        return {"status": "error", "reason": "message is required"}
    try:
        thread = int(thread_id)
    except (TypeError, ValueError):
        return {"status": "error", "reason": "thread_id (the existing topic's message_thread_id) is required and must be numeric"}

    token = settings.telegram_bot_api_key
    if not token:
        return {"status": "error", "reason": "TELEGRAM_BOT_API_KEY not configured on this box"}

    target = (chat_id or _chat_id_from_session(session_id)
              or (str(settings.telegram_home_group_id) if settings.telegram_home_group_id else None))
    if not target:
        return {"status": "error",
                "reason": "no target group — not in a Telegram topic session and "
                          "TELEGRAM_HOME_GROUP_ID is unset. Pass chat_id."}

    try:
        r = httpx.post(f"{_API}/bot{token}/sendMessage",
                       json={"chat_id": target, "message_thread_id": thread, "text": message},
                       timeout=_TIMEOUT)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"sendMessage call failed: {e}"}

    if not data.get("ok"):
        desc = data.get("description", "unknown error")
        hint = ("Verify the thread_id is a real topic in this group and Sophia's "
                "bot can post there (group admin / not restricted).")
        return {"status": "error", "reason": f"Telegram: {desc}", "hint": hint,
                "chat_id": target, "message_thread_id": thread}

    logger.info("posted to existing topic (thread=%s) in chat %s", thread, target)
    return {
        "status": "ok",
        "message_thread_id": thread,
        "chat_id": target,
        "message_id": (data.get("result") or {}).get("message_id"),
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
            "message": {"type": "string", "description": "The message text to post into the topic."},
            "thread_id": {"type": "integer", "description": "The existing topic's message_thread_id (e.g. 1955)."},
            "chat_id": {"type": "string", "description": "Optional explicit group chat id; defaults to current/working group."},
        },
        "required": ["message", "thread_id"],
    },
    handler=lambda args, ctx: json.dumps(post_to_telegram_topic(
        message=args.get("message", ""),
        thread_id=args.get("thread_id"),
        chat_id=args.get("chat_id"),
        session_id=ctx.get("session_id"),
    ), indent=2),
    default_roles=None,  # uniform — same as create_telegram_topic
)
