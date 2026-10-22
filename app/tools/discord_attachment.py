"""Post messages and file attachments into a Discord channel.

Discord parity with the Telegram attachment surface. The Discord adapter
(``app/discord_adapter.py``) could already *reply* inside a live session
(``send_message``) and upload voice notes (``send_voice``), but there was no
**agent-callable tool** to post a message or an arbitrary file into an explicit
channel — so a governor asking "drop that PDF in the channel" had to fall back
to a GitHub link (which the Great Firewall may block) or a manual upload. This
module closes that gap.

Two tools (registered in ``TOOL_SPECS``):

    post_discord_message(message, channel_id=..., session_id=...)        # text, 2000-char chunks
    send_discord_attachment(file_path, caption=..., channel_id=...)      # any file <= 25 MB

Target channel resolution order (mirrors ``discord_admin``): explicit
``channel_id`` arg -> the channel in a ``dc:{guild}:{channel}`` session id.

**Safety:** both are registered as **WRITE** in ``app/policy.py`` (governor
gate, Security invariant #1) — the same class as ``post_to_telegram_topic``.
Discord's own ``DISCORD_DRY_RUN`` setting short-circuits posting when on.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from ..config import settings
from ..tool_registry import ToolSpec

logger = logging.getLogger("autopilot.tools.discord_attachment")

_MAX_BYTES = 25 * 1024 * 1024  # Discord default per-file upload limit
_CAPTION_LIMIT = 2000  # message content limit for the file's caption
_TIMEOUT = 120.0  # file upload — allow longer than the adapter's plain-text call


def _channel_id_from_session(session_id: str | None) -> str | None:
    """Recover the channel id from a ``dc:{guild}:{channel}`` session id.

    ``build_session_id`` in ``discord_adapter.py`` writes this shape; the brain
    may prefix it, so look for the ``dc`` token anywhere. Mirrors
    ``_guild_id_from_session`` in ``discord_admin.py``, one segment over.
    """
    if not session_id:
        return None
    parts = session_id.split(":")
    if "dc" in parts:
        i = parts.index("dc")
        if i + 2 < len(parts) and parts[i + 2]:
            return parts[i + 2]
    return None


def _resolve_channel_id(channel_id: str | None, session_id: str | None) -> str | None:
    explicit = (channel_id or "").strip()
    if explicit:
        return explicit
    return _channel_id_from_session(session_id)


def _upload_file(cid: str, path: str, caption: str | None) -> dict | None:
    """Multipart upload of ``path`` as ``files[0]`` to channel ``cid``.

    Returns the parsed message JSON on 200/201, else None (logged).
    """
    from ..discord_adapter import _DISCORD_API, _USER_AGENT, get_token

    url = f"{_DISCORD_API}/channels/{cid}/messages"
    headers = {"Authorization": f"Bot {get_token()}", "User-Agent": _USER_AGENT}
    data = {"content": (caption or "")[:_CAPTION_LIMIT]} if caption else {}
    with open(path, "rb") as fh:
        files = {"files[0]": (os.path.basename(path), fh, "application/octet-stream")}
        resp = httpx.post(
            url, headers=headers, data=data, files=files, timeout=_TIMEOUT
        )
    if resp.status_code in (200, 201):
        return resp.json()
    logger.warning("Discord file upload -> %s: %s", resp.status_code, resp.text[:200])
    return None


def post_discord_message(
    message: str,
    channel_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Post a text message into a Discord channel (auto-chunked at 2000 chars)."""
    text = (message or "").strip()
    if not text:
        return {"status": "error", "reason": "message is required"}

    cid = _resolve_channel_id(channel_id, session_id)
    if not cid:
        return {
            "status": "error",
            "reason": "no target channel — pass channel_id or be in a dc: session",
        }

    from ..discord_adapter import _api, chunk_text

    ids: list[str] = []
    for chunk in chunk_text(text):
        if settings.discord_dry_run:
            logger.info("[DRY_RUN] would post to channel %s: %.120s", cid, chunk)
            ids.append("dry-run")
            continue
        res = _api("POST", f"/channels/{cid}/messages", {"content": chunk})
        if not res or not res.get("id"):
            return {
                "status": "error",
                "reason": "Discord post-message call failed",
                "hint": "Ensure Sophia's bot can View Channel + Send Messages here.",
                "channel_id": cid,
                "posted": ids,
            }
        ids.append(res["id"])
    logger.info("posted %d message chunk(s) to Discord channel %s", len(ids), cid)
    return {"status": "ok", "channel_id": cid, "message_ids": ids, "chunks": len(ids)}


def send_discord_attachment(
    file_path: str,
    caption: str | None = None,
    channel_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Upload a local file (PDF, image, etc.) as a Discord message attachment."""
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
                f"file is {size} bytes — exceeds Discord's {_MAX_BYTES}-byte default "
                "upload limit. Compress/split it, or raise the guild's file-size tier."
            ),
        }

    # Self-routing: a Telegram conversation carries no dc: channel, so a governor
    # asking "attach it here" from Telegram would otherwise get a hard
    # "no target channel". Symmetric to the guard in send_telegram_attachment.
    from .delivery_target import describe_destination, platform_from_session

    if (
        not (channel_id or "").strip()
        and platform_from_session(session_id) == "telegram"
    ):
        from .telegram_attachment import send_telegram_attachment

        logger.info(
            "send_discord_attachment: session is Telegram -- routing to "
            "send_telegram_attachment (file=%s)",
            path,
        )
        result = send_telegram_attachment(
            file_path=path, caption=caption, session_id=session_id
        )
        result.setdefault("routed_from", "send_discord_attachment")
        result.setdefault("destination", describe_destination(session_id))
        return result

    cid = _resolve_channel_id(channel_id, session_id)
    if not cid:
        return {
            "status": "error",
            "reason": "no target channel — pass channel_id or be in a dc: session",
        }

    if settings.discord_dry_run:
        logger.info("[DRY_RUN] would send attachment to %s: %s", cid, path)
        return {
            "status": "ok",
            "dry_run": True,
            "channel_id": cid,
            "file": os.path.basename(path),
        }

    try:
        data = _upload_file(cid, path, caption)
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "reason": f"Discord file upload failed: {e}",
            "channel_id": cid,
        }
    if not data:
        return {
            "status": "error",
            "reason": "Discord file upload rejected",
            "hint": "Ensure the bot can Send Messages + Attach Files here.",
            "channel_id": cid,
        }

    atts = data.get("attachments") or []
    logger.info("sent attachment %s (%d bytes) to Discord channel %s", path, size, cid)
    return {
        "status": "ok",
        "channel_id": cid,
        "message_id": data.get("id"),
        "file": os.path.basename(path),
        "size_bytes": size,
        "attachment_url": atts[0].get("url") if atts else None,
    }


# ── Tool specs ────────────────────────────────────────────────────────────

_POST_SPEC = ToolSpec(
    name="post_discord_message",
    description=(
        "Post a text message into a Discord channel. Use this to deliver a "
        "report/summary INTO Discord itself (instead of only a GitHub link). "
        "Auto-chunks at Discord's 2000-char limit. Target the channel via "
        "channel_id, or omit it to use the current dc: session's channel."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message text to post."},
            "channel_id": {
                "type": "string",
                "description": "Target channel id; defaults to the current dc: session's channel.",
            },
        },
        "required": ["message"],
    },
    handler=lambda args, ctx: json.dumps(
        post_discord_message(
            message=args.get("message", ""),
            channel_id=args.get("channel_id"),
            session_id=ctx.get("session_id"),
        ),
        indent=2,
    ),
    default_roles=None,
)

_ATTACH_SPEC = ToolSpec(
    name="send_discord_attachment",
    description=(
        "Upload a local file (PDF, image, etc.) as a Discord message attachment "
        "into a channel. Use this instead of a link when the governor needs the "
        "ACTUAL FILE in Discord. Target the channel via channel_id, or omit it to "
        "use the current dc: session's channel. Max 25 MB."
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
                "description": "Optional caption shown with the file.",
            },
            "channel_id": {
                "type": "string",
                "description": "Target channel id; defaults to the current dc: session's channel.",
            },
        },
        "required": ["file_path"],
    },
    handler=lambda args, ctx: json.dumps(
        send_discord_attachment(
            file_path=args.get("file_path", ""),
            caption=args.get("caption"),
            channel_id=args.get("channel_id"),
            session_id=ctx.get("session_key") or ctx.get("session_id"),
        ),
        indent=2,
    ),
    default_roles=None,
)

TOOL_SPECS = [_POST_SPEC, _ATTACH_SPEC]
