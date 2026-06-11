"""Create a Telegram forum **topic** in the working group — Sophia's hook for
the local-LLM → Sophia execution handoff.

A governor crafts an implementation plan + execution roadmap with a local LLM,
commits the roadmap to ``agentic_ai_context`` (the tracked baton), then triggers
Sophia (e.g. via ``dao_client``'s ``ping_sophia``). Sophia calls this tool to
open a dedicated topic for that execution and post a kickoff, so the governor
can step into Telegram and carry on the conversation in a clean, isolated
thread (the adapter already keys one autopilot session per topic).

Requirements (one-time, operator):
  * The group must have **Topics enabled** (forum group).
  * Sophia's bot must be a **group admin with the "Manage Topics" right**.
Without these the Bot API returns an error, surfaced with a fix hint.

chat_id resolution (in order):
  1. explicit ``chat_id`` arg,
  2. the current Telegram topic's chat (when invoked from a ``tg:`` session),
  3. ``settings.telegram_home_group_id`` (the configured working group) — this
     is what the off-Telegram ``/chat`` handoff trigger relies on.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..config import settings
from ..tool_registry import ToolSpec

logger = logging.getLogger("autopilot.tools.telegram_topic")

_API = "https://api.telegram.org"
_TIMEOUT = 20.0


def _chat_id_from_session(session_id: str | None) -> str | None:
    """Recover the Telegram chat id from a ``…:tg:{chat}:{thread}`` session id."""
    if not session_id:
        return None
    parts = session_id.split(":")
    if "tg" in parts:
        i = parts.index("tg")
        if i + 1 < len(parts) and parts[i + 1]:
            return parts[i + 1]
    return None


def _deep_link(chat_id: str, thread_id: int) -> str:
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{thread_id}"
    return ""


def create_telegram_topic(
    name: str, kickoff_message: str = "", chat_id: str | None = None, session_id: str | None = None
) -> dict:
    name = (name or "").strip()
    if not name:
        return {"status": "error", "reason": "topic name is required"}
    token = settings.telegram_bot_api_key
    if not token:
        return {"status": "error", "reason": "TELEGRAM_BOT_API_KEY not configured on this box"}

    target = (
        chat_id
        or _chat_id_from_session(session_id)
        or (str(settings.telegram_home_group_id) if settings.telegram_home_group_id else None)
    )
    if not target:
        return {
            "status": "error",
            "reason": "no target group — not in a Telegram topic session and "
            "TELEGRAM_HOME_GROUP_ID is unset. Set the working group id or pass chat_id.",
        }

    try:
        r = httpx.post(
            f"{_API}/bot{token}/createForumTopic", json={"chat_id": target, "name": name[:128]}, timeout=_TIMEOUT
        )
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"createForumTopic call failed: {e}"}

    if not data.get("ok"):
        desc = data.get("description", "unknown error")
        hint = (
            "Ensure the group has Topics enabled AND Sophia's bot is a group admin with the 'Manage Topics' permission."
        )
        return {"status": "error", "reason": f"Telegram: {desc}", "hint": hint, "chat_id": target}

    thread_id = data["result"]["message_thread_id"]
    posted = False
    if kickoff_message.strip():
        try:
            pr = httpx.post(
                f"{_API}/bot{token}/sendMessage",
                json={"chat_id": target, "message_thread_id": thread_id, "text": kickoff_message},
                timeout=_TIMEOUT,
            )
            posted = bool(pr.json().get("ok"))
        except Exception as e:  # noqa: BLE001
            logger.warning("kickoff sendMessage failed: %s", e)

    link = _deep_link(target, thread_id)
    logger.info("created topic %r (thread=%s) in chat %s", name, thread_id, target)
    return {
        "status": "ok",
        "topic_name": name,
        "message_thread_id": thread_id,
        "chat_id": target,
        "kickoff_posted": posted,
        "link": link,
    }


TOOL_SPEC = ToolSpec(
    name="create_telegram_topic",
    description=(
        "Create a new Telegram forum TOPIC in the working group and optionally "
        "post a kickoff message in it. Use this when a governor hands off an "
        "execution plan and wants a dedicated topic to monitor it in — open the "
        "topic, then post a short kickoff summarizing what you've taken over "
        "(reference the roadmap file you'll work from). Requires Sophia's bot to "
        "be a group admin with 'Manage Topics' and the group to have Topics "
        "enabled. The target group defaults to the configured working group when "
        "you're not already inside a Telegram topic."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Topic title (e.g. 'Exec: warm-up auto-send')."},
            "kickoff_message": {"type": "string", "description": "Optional first message to post in the new topic."},
            "chat_id": {
                "type": "string",
                "description": "Optional explicit group chat id; defaults to current group / configured working group.",
            },
        },
        "required": ["name"],
    },
    handler=lambda args, ctx: json.dumps(
        create_telegram_topic(
            name=args.get("name", ""),
            kickoff_message=args.get("kickoff_message", ""),
            chat_id=args.get("chat_id"),
            session_id=ctx.get("session_id"),
        ),
        indent=2,
    ),
    default_roles=None,  # uniform — any role a governor is in can hand off
)
