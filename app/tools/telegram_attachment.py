"""Send a local file as a Telegram document attachment.

Companion to send_voice/send_message in telegram_adapter.py — lets Sophia hand
the governor an ACTUAL FILE (a generated PDF report, a downloaded GitHub
asset, etc.) instead of only a link. Added 2026-07-26: the governor is often
behind the Great Firewall, where github.com / the AWS console / etc. are
blocked outright, but Telegram itself gets through — attaching the file
sidesteps the block entirely instead of handing over a link that just won't
load.

Defaults to the CURRENT conversation's chat/thread (derived from session_id),
so Sophia doesn't need to know/guess IDs to answer "attach the file you just
made" — chat_id/thread_id are optional overrides for posting into a
DIFFERENT conversation than the one she's currently in.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from ..config import settings
from ..tool_registry import ToolSpec
from .telegram_topic import _API, _chat_id_from_session

logger = logging.getLogger("autopilot.tools.telegram_attachment")

_MAX_BYTES = 50 * 1024 * 1024  # Telegram Bot API sendDocument hard limit
_CAPTION_LIMIT = 1024  # Telegram caption hard limit
_TIMEOUT = 60.0  # file upload — allow longer than the plain-text _TIMEOUT


def _thread_id_from_session(session_id: str | None) -> int | None:
    """Recover the Telegram thread id from a ``…:tg:{chat}:{thread}`` session id
    (mirrors _chat_id_from_session in telegram_topic.py, one segment over)."""
    if not session_id:
        return None
    parts = session_id.split(":")
    if "tg" in parts:
        i = parts.index("tg")
        if i + 2 < len(parts) and parts[i + 2]:
            try:
                thread = int(parts[i + 2])
            except ValueError:
                return None
            return thread or None  # "0" means General/no-topic — treat as None
    return None


def _post_document(
    token: str, target: str, path: str, thread: int | None, caption: str | None
) -> dict:
    with open(path, "rb") as fh:
        files = {"document": (os.path.basename(path), fh, "application/octet-stream")}
        payload: dict = {"chat_id": target}
        if thread:
            payload["message_thread_id"] = thread
        if caption:
            payload["caption"] = caption[:_CAPTION_LIMIT]
        resp = httpx.post(
            f"{_API}/bot{token}/sendDocument", data=payload, files=files, timeout=_TIMEOUT
        )
    return resp.json()


def send_telegram_attachment(
    file_path: str,
    caption: str | None = None,
    chat_id: str | None = None,
    thread_id: int | str | None = None,
    session_id: str | None = None,
) -> dict:
    path = (file_path or "").strip()
    if not path:
        return {"status": "error", "reason": "file_path is required"}
    if not os.path.isfile(path):
        return {"status": "error", "reason": f"file not found: {path}"}

    size = os.path.getsize(path)
    if size == 0:
        return {"status": "error", "reason": f"file is empty: {path}"}
    if size > _MAX_BYTES:
        return {
            "status": "error",
            "reason": (
                f"file is {size} bytes — exceeds Telegram's {_MAX_BYTES}-byte "
                "sendDocument limit. Compress it or split it before attaching."
            ),
        }

    token = settings.telegram_bot_api_key
    if not token:
        return {"status": "error", "reason": "TELEGRAM_BOT_API_KEY not configured on this box"}

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
            "reason": "no target chat — not in a Telegram session and "
            "TELEGRAM_HOME_GROUP_ID is unset. Pass chat_id.",
        }

    if thread_id is not None:
        try:
            thread = int(thread_id)
        except (TypeError, ValueError):
            return {"status": "error", "reason": "thread_id must be numeric"}
    else:
        thread = _thread_id_from_session(session_id)

    try:
        data = _post_document(token, target, path, thread, caption)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"sendDocument call failed: {e}"}

    if not data.get("ok"):
        desc = data.get("description", "unknown error")
        # A common failure mode: the topic this session remembers no longer
        # exists (deleted/closed). Retry once without message_thread_id so the
        # file still reaches the chat instead of silently failing.
        if thread and "thread not found" in desc.lower():
            try:
                data = _post_document(token, target, path, None, caption)
            except Exception:  # noqa: BLE001
                data = {"ok": False, "description": desc}
            if data.get("ok"):
                logger.info(
                    "sent attachment %s to chat %s (fallback: original thread %s not found)",
                    path, target, thread,
                )
                return {
                    "status": "ok",
                    "chat_id": target,
                    "message_id": (data.get("result") or {}).get("message_id"),
                    "note": f"posted without thread_id — thread {thread} was not found",
                }
        return {"status": "error", "reason": f"Telegram: {desc}", "chat_id": target}

    logger.info(
        "sent attachment %s (%d bytes) to chat %s thread %s", path, size, target, thread
    )
    return {
        "status": "ok",
        "chat_id": target,
        "message_thread_id": thread,
        "message_id": (data.get("result") or {}).get("message_id"),
    }


TOOL_SPEC = ToolSpec(
    name="send_telegram_attachment",
    description=(
        "Send a local file (one you've already generated or downloaded on this "
        "box — a PDF report, an exported doc, etc.) as a Telegram document "
        "attachment INTO THE CURRENT conversation. Use this instead of sharing "
        "a link when the governor is behind a firewall that blocks github.com / "
        "the AWS console / etc. — attaching the actual file sidesteps that "
        "entirely. Defaults to the current chat/thread; only pass chat_id/"
        "thread_id to post into a DIFFERENT conversation. Max 50MB (Telegram's "
        "sendDocument limit) — compress or split larger files first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute local path to the file to attach.",
            },
            "caption": {
                "type": "string",
                "description": "Optional short caption shown with the file (Telegram limit ~1024 chars).",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional explicit group chat id; defaults to the current conversation.",
            },
            "thread_id": {
                "type": "integer",
                "description": "Optional explicit topic thread_id; defaults to the current conversation's thread.",
            },
        },
        "required": ["file_path"],
    },
    handler=lambda args, ctx: json.dumps(
        send_telegram_attachment(
            file_path=args.get("file_path", ""),
            caption=args.get("caption"),
            chat_id=args.get("chat_id"),
            thread_id=args.get("thread_id"),
            session_id=ctx.get("session_id"),
        ),
        indent=2,
    ),
    default_roles=None,  # uniform — any role should be able to hand back a file
)
