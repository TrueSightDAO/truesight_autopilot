"""Telegram front-end for the autopilot chat.

A standalone, single-instance long-poller (NOT a web-app background task — the
uvicorn app runs multiple workers, which would race on getUpdates). Run it as
its own systemd unit:  python -m app.telegram_adapter

Flow per message:
  1. Drop anything from a Telegram user not on the allowlist (the security gate).
  2. Mint a short-lived JWT for the configured governor's public key (resolved
     from the DAO governor registry) so /chat-blocking knows it's "Gary Teh".
  3. POST the text to /chat-blocking with an X-Session-Id derived from the
     Telegram chat + topic (so each topic is its own conversation).
  4. Send the assistant's reply back to the same chat/topic.

Security model: the trust boundary is the Telegram user-ID allowlist. The
adapter runs on the same host as the FastAPI service and holds JWT_SECRET, so
minting its own JWT is equivalent to any other trusted server-side code.
"""
from __future__ import annotations

import concurrent.futures
import html
import logging
import re
import time
from typing import Any

import httpx

from .auth import create_jwt
from .config import settings
from .governor_registry import load_governors

logger = logging.getLogger("autopilot.telegram")

_TELEGRAM_API = "https://api.telegram.org"
_MESSAGE_LIMIT = 4096          # Telegram hard cap per message
_CHAT_TIMEOUT = 180.0          # /chat-blocking can run tools + multiple LLM calls
_POLL_TIMEOUT = 30             # long-poll seconds
_TYPING_INTERVAL = 4.0         # Telegram's "typing…" lasts ~5s; refresh under that


# ── Pure helpers (unit-tested) ─────────────────────────────────────────────

def parse_allowed_ids(raw: str) -> set[int]:
    """Parse 'TELEGRAM_ALLOWED_USER_IDS' into a set of ints. Ignores blanks/junk."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def is_allowed(user_id: int, allowed: set[int]) -> bool:
    """True only if the allowlist is configured AND the user is on it."""
    return bool(allowed) and user_id in allowed


def build_session_id(chat_id: int, thread_id: int | None) -> str:
    """Map a Telegram chat (+ forum topic) to a stable autopilot session id."""
    return f"tg:{chat_id}:{thread_id or 0}"


def chunk_text(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split a long reply into Telegram-sized chunks, preferring line breaks."""
    text = text if (text and text.strip()) else "(no response)"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_US_RE = re.compile(r"__([^_\n]+)__")
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")


def markdown_to_telegram_html(md: str) -> str:
    """Convert the subset of Markdown LLMs emit into Telegram-supported HTML.

    Telegram has no headings, uses <b>/<i>/<code>/<pre>/<a>, and only needs
    &, <, > escaped in text. Strategy: stash code (escaped) behind placeholders,
    escape the rest, then map headings/bold/bullets/links to tags, then restore.
    """
    stash: dict[str, str] = {}

    def _store(inner: str, tag: str) -> str:
        key = f"@@TGCODE{len(stash)}@@"
        stash[key] = f"<{tag}>{html.escape(inner, quote=False)}</{tag}>"
        return key

    text = _FENCE_RE.sub(lambda m: _store(m.group(1).rstrip("\n"), "pre"), md)
    text = _INLINE_CODE_RE.sub(lambda m: _store(m.group(1), "code"), text)
    text = html.escape(text, quote=False)                       # escape remaining &<>
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    lines: list[str] = []
    for line in text.split("\n"):
        h = _HEADER_RE.match(line)
        if h:
            # header is already bold; strip inner **/__ so the bold pass below
            # doesn't produce invalid nested <b><b>…</b></b> (Telegram 400s on it)
            inner = h.group(1).replace("**", "").replace("__", "")
            lines.append(f"<b>{inner}</b>")
            continue
        b = _BULLET_RE.match(line)
        if b:
            lines.append(f"{b.group(1)}• {b.group(2)}")
            continue
        lines.append(line)
    text = "\n".join(lines)

    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_US_RE.sub(r"<b>\1</b>", text)

    for key, val in stash.items():
        text = text.replace(key, val)
    return text


# ── Telegram + chat I/O ────────────────────────────────────────────────────

def _api(method: str) -> str:
    return f"{_TELEGRAM_API}/bot{settings.telegram_bot_api_key}/{method}"


def get_updates(offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": _POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = httpx.get(_api("getUpdates"), params=params, timeout=_POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_message(chat_id: int, text: str, thread_id: int | None = None) -> None:
    for chunk in chunk_text(text):
        # Render Markdown → Telegram HTML so headings/bold/bullets/code show up.
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": markdown_to_telegram_html(chunk),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            resp = httpx.post(_api("sendMessage"), json=payload, timeout=20.0)
            if resp.status_code != 200:
                logger.warning("sendMessage %s: %s", resp.status_code, resp.text[:200])
                # Fallback: send the raw chunk as plain text, no thread. Covers both
                # "message thread not found" and any HTML parse error — the reply
                # still lands (just unformatted) instead of vanishing.
                fallback = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
                httpx.post(_api("sendMessage"), json=fallback, timeout=20.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", e)


def send_typing(chat_id: int, thread_id: int | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        httpx.post(_api("sendChatAction"), json=payload, timeout=10.0)
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def resolve_governor_public_key() -> str | None:
    """Find the public key of the configured governor name in the DAO registry."""
    target = settings.telegram_governor_name.strip()
    for g in load_governors().get("governors", []):
        if g.get("name", "").strip() == target and g.get("public_key"):
            return g["public_key"]
    return None


def call_chat(message: str, session_id: str, public_key: str) -> str:
    """POST to /chat-blocking as the governor; return the assistant text."""
    token = create_jwt(public_key)
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
    resp = httpx.post(
        f"{settings.autopilot_chat_url.rstrip('/')}/chat-blocking",
        json={"message": message},
        headers=headers,
        timeout=_CHAT_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("chat-blocking HTTP %s: %s", resp.status_code, resp.text[:300])
        return f"⚠️ Autopilot returned HTTP {resp.status_code}."
    data = resp.json()
    text = (data.get("response") or "").strip()
    if not text:
        text = "⚠️ Autopilot returned an empty response. Try rephrasing, or break the request into smaller steps."
    if data.get("proposal"):
        text += "\n\n⚠️ This action needs approval — open the DApp chat to approve/reject."
    return text


def call_chat_with_typing(chat_id: int, thread_id: int | None, message: str,
                          session_id: str, public_key: str) -> str:
    """Run call_chat in a worker thread, re-sending the 'typing…' action every
    few seconds so the indicator stays alive for the whole (often 30-60s+)
    multi-round generation instead of vanishing after ~5s."""
    send_typing(chat_id, thread_id)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(call_chat, message, session_id, public_key)
        while True:
            try:
                return future.result(timeout=_TYPING_INTERVAL)
            except concurrent.futures.TimeoutError:
                send_typing(chat_id, thread_id)  # keep the indicator alive


# ── Update handling + loop ─────────────────────────────────────────────────

def handle_message(msg: dict[str, Any], allowed: set[int], public_key: str | None) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    # Only treat a thread id as routable when it's a genuine forum topic.
    # Reply-threads and the General topic carry ids that 400 on sendMessage
    # ("message thread not found"), so we ignore them for routing + session keying.
    thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or user_id is None or not text:
        return

    # Security gate
    if not is_allowed(user_id, allowed):
        if not allowed:
            # Bootstrap: no allowlist configured yet — reveal the sender's own ID.
            logger.warning("Unconfigured allowlist; message from user_id=%s", user_id)
            send_message(chat_id,
                         f"Your Telegram user ID is {user_id}.\n"
                         f"Add it to TELEGRAM_ALLOWED_USER_IDS and restart to enable me.",
                         thread_id)
        else:
            logger.warning("Rejected message from non-allowlisted user_id=%s", user_id)
            send_message(chat_id, "⛔ Not authorized.", thread_id)
        return

    # Lightweight commands
    if text in ("/start", "/help"):
        send_message(chat_id,
                     "TrueSight Autopilot — your private DAO assistant.\n"
                     "Just type what you want. Each Telegram topic is a separate context.\n"
                     "I can read the codebase/context, search the web, and open PRs.",
                     thread_id)
        return

    if public_key is None:
        send_message(chat_id, "⚠️ No governor identity configured on the server.", thread_id)
        return

    session_id = build_session_id(chat_id, thread_id)
    try:
        reply = call_chat_with_typing(chat_id, thread_id, text, session_id, public_key)
    except Exception as e:  # noqa: BLE001 — never crash the loop on one message
        logger.exception("call_chat failed")
        reply = f"⚠️ Error talking to autopilot: {e}"
    send_message(chat_id, reply, thread_id)


def run() -> None:
    if not settings.telegram_bot_api_key:
        raise SystemExit("TELEGRAM_BOT_API_KEY is not set — cannot start Telegram adapter.")

    allowed = parse_allowed_ids(settings.telegram_allowed_user_ids)
    public_key = resolve_governor_public_key()
    logger.info("Telegram adapter starting: allowlist=%s governor=%s key_resolved=%s",
                sorted(allowed) or "(BOOTSTRAP — none set)", settings.telegram_governor_name,
                public_key is not None)
    if not allowed:
        logger.warning("No TELEGRAM_ALLOWED_USER_IDS set — running in bootstrap mode "
                       "(replies with the sender's ID; does not call autopilot).")
    if public_key is None:
        logger.warning("Could not resolve a public key for governor '%s' — chat calls will be refused.",
                       settings.telegram_governor_name)

    offset: int | None = None
    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:  # noqa: BLE001
            logger.warning("getUpdates failed: %s — backing off 5s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg:
                try:
                    handle_message(msg, allowed, public_key)
                except Exception:  # noqa: BLE001
                    logger.exception("handle_message crashed on update %s", upd.get("update_id"))


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
