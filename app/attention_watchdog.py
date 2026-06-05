"""Telegram attention watchdog — Sophia nudges Gary about unanswered asks.

Born from a concrete miss: a cacao serving scheduled for 2026-06-12 was
cancelled because a time-sensitive coordination message on a Telegram channel
went unanswered (see agentic_ai_context/OPEN_FOLLOWUPS.md "Telegram attention
watchdog"). The bot adapter can't watch for this — bots never see DMs and only
see groups they're added to — so this runs as a **read-only MTProto
user-session** (Telethon) of the operator's own account.

Scope is deliberately minimal (operator decision 2026-06-06):

  * Track an incoming message as "awaiting Gary" when it is a DM, mentions
    him, or replies to one of his messages (Telegram sets ``Message.mentioned``
    for the last two), AND it looks like an ask — a question mark or a
    date/time reference. Pure heuristics; **no LLM, message content never
    leaves this box.**
  * Clear the chat's pending state the moment Gary posts anything in it.
  * Nudge via **Saved Messages** (self-DM) after WATCHDOG_NUDGE_HOURS
    (default 4 h; 2 h when the ask carries a date — those are the ones that
    cancel events). At most one nudge per chat per day.
  * Daily digest of everything still outstanding at WATCHDOG_DIGEST_HOUR
    local time.
  * Never sends a message to anyone but the operator himself. No
    auto-replies, ever.

Run as its own single-instance systemd unit (like the bot adapter):

    python -m app.attention_watchdog

First-time setup: ``scripts/telethon_login.py`` performs the one-time
interactive login that creates the session file; this service refuses to
start without an authorized session (deploy.sh gates on the file existing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings

logger = logging.getLogger("autopilot.watchdog")

_POLL_SECONDS = 300          # nudge/digest evaluation cadence
_PENDING_TTL_DAYS = 7        # drop unanswered items after a week (stale)
_SNIPPET_CHARS = 160
_RENUDGE_HOURS = 24.0        # max one nudge per chat per day

# ── Ask heuristics (pure, unit-tested) ──────────────────────────────────────

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|"
    "march|april|june|july|august|september|october|november|december"
)
_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
DATE_RE = re.compile(
    rf"\b(?:{_MONTHS})\b\.?\s*\d{{0,2}}"          # "June 12", "Jun.", "September"
    rf"|\b(?:{_WEEKDAYS})\b"                       # "Friday"
    r"|\b(?:tomorrow|tonight|today|next week|this week(?:end)?)\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b"                 # "the 12th"
    r"|\b\d{1,2}[:.]\d{2}\s*(?:am|pm)?\b"          # "3:30pm", "15.00"
    r"|\b\d{1,2}\s*(?:am|pm)\b"                    # "3pm"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",          # "6/12"
    re.IGNORECASE,
)
_QUESTIONY_RE = re.compile(
    r"\b(?:can you|could you|will you|are you|do you|did you|would you|"
    r"please confirm|let me know|lmk|confirm|rsvp|still on|are we|"
    r"work for you|works for you|any chance|what time|need to know|"
    r"need you (?:there|by|at|to))\b",
    re.IGNORECASE,
)


def classify_text(text: str) -> tuple[bool, bool]:
    """(is_ask, is_dated). An ask must be question-shaped — '?' or asky
    phrasing. A date reference alone is NOT an ask (people mention "today"
    in smalltalk constantly); dates only tighten the nudge SLA on messages
    that already ask, because dated asks are the ones that cancel events."""
    t = (text or "").strip()
    if not t:
        return False, False
    ask = ("?" in t) or bool(_QUESTIONY_RE.search(t))
    dated = ask and bool(DATE_RE.search(t))
    return ask, dated


def should_track(
    *, is_private: bool, mentioned: bool, sender_is_bot: bool,
    is_broadcast: bool, text: str,
) -> tuple[bool, bool]:
    """(track, dated) — the full gate, pure so tests can sweep it."""
    if sender_is_bot or is_broadcast:
        return False, False
    if not (is_private or mentioned):
        return False, False
    ask, dated = classify_text(text)
    return ask, dated


def chat_deep_link(chat_id: int, msg_id: int, username: str | None) -> str:
    """Best-effort clickable pointer to the message."""
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):                       # supergroup internal form
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return ""                                      # plain DMs: title is enough


# ── Persistent state ────────────────────────────────────────────────────────

class State:
    """pending[chat_id] = {chat_title, sender, snippet, msg_id, link,
    detected_at, dated, last_nudge_at} — JSON on disk, tiny."""

    def __init__(self, path: Path):
        self.path = path
        self.pending: dict[str, dict] = {}
        self.last_digest_date: str = ""
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.pending = raw.get("pending", {})
                self.last_digest_date = raw.get("last_digest_date", "")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("state file unreadable (%s) — starting fresh", e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "pending": self.pending,
            "last_digest_date": self.last_digest_date,
        }, indent=1), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(iso: str) -> float:
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return 0.0


def _fmt_item(item: dict) -> str:
    age = _age_hours(item["detected_at"])
    flag = " 📅" if item.get("dated") else ""
    line = (f"• {item['chat_title']}{flag} — {item['sender']}, "
            f"{age:.1f}h ago: “{item['snippet']}”")
    if item.get("link"):
        line += f"\n  {item['link']}"
    return line


# ── Service ─────────────────────────────────────────────────────────────────

async def run() -> int:
    # Imported here so the pure helpers above stay importable in test envs
    # that don't install Telethon.
    from telethon import TelegramClient, events

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        logger.error("TELEGRAM_API_ID / TELEGRAM_API_HASH not set — see "
                     "scripts/telethon_login.py docstring. Exiting.")
        return 1

    client = TelegramClient(
        settings.watchdog_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        logger.error("Telethon session not authorized — run "
                     ".venv/bin/python scripts/telethon_login.py once. Exiting.")
        await client.disconnect()
        return 1

    me = await client.get_me()
    logger.info("watchdog up as %s (id=%s)", me.username or me.first_name, me.id)
    state = State(Path(settings.watchdog_state_path))
    tz = ZoneInfo(settings.watchdog_tz)

    @client.on(events.NewMessage(incoming=True))
    async def on_incoming(event):  # noqa: ANN001
        try:
            msg = event.message
            is_broadcast = bool(event.is_channel and not event.is_group)
            sender = await event.get_sender()
            sender_is_bot = bool(getattr(sender, "bot", False))
            track, dated = should_track(
                is_private=bool(event.is_private),
                mentioned=bool(getattr(msg, "mentioned", False)),
                sender_is_bot=sender_is_bot,
                is_broadcast=is_broadcast,
                text=msg.message or "",
            )
            if not track:
                return
            chat = await event.get_chat()
            chat_key = str(event.chat_id)
            title = (getattr(chat, "title", None)
                     or " ".join(filter(None, [getattr(chat, "first_name", None),
                                               getattr(chat, "last_name", None)]))
                     or chat_key)
            sender_name = " ".join(filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])) or getattr(sender, "username", None) or "someone"
            existing = state.pending.get(chat_key)
            if existing:
                # Keep the earliest detection (the SLA clock), refresh snippet,
                # upgrade to dated if the newer message carries a date.
                existing["snippet"] = (msg.message or "")[:_SNIPPET_CHARS]
                existing["dated"] = existing.get("dated") or dated
            else:
                state.pending[chat_key] = {
                    "chat_title": title,
                    "sender": sender_name,
                    "snippet": (msg.message or "")[:_SNIPPET_CHARS],
                    "msg_id": msg.id,
                    "link": chat_deep_link(event.chat_id, msg.id,
                                           getattr(chat, "username", None)),
                    "detected_at": _now().isoformat(),
                    "dated": dated,
                    "last_nudge_at": "",
                }
                logger.info("tracking: %s (dated=%s)", title, dated)
            state.save()
        except Exception:  # noqa: BLE001 — watchdog must never crash on one message
            logger.exception("on_incoming failed")

    @client.on(events.NewMessage(outgoing=True))
    async def on_outgoing(event):  # noqa: ANN001
        try:
            chat_key = str(event.chat_id)
            if chat_key == str(me.id):
                return  # our own Saved Messages nudges must not clear anything
            if state.pending.pop(chat_key, None) is not None:
                logger.info("cleared: chat %s (operator replied)", chat_key)
                state.save()
        except Exception:  # noqa: BLE001
            logger.exception("on_outgoing failed")

    async def nudge_loop():
        while True:
            try:
                now = _now()
                changed = False
                for chat_key, item in list(state.pending.items()):
                    age_h = _age_hours(item["detected_at"])
                    if age_h > _PENDING_TTL_DAYS * 24:
                        state.pending.pop(chat_key)
                        changed = True
                        continue
                    due_h = (settings.watchdog_urgent_nudge_hours
                             if item.get("dated") else settings.watchdog_nudge_hours)
                    last = item.get("last_nudge_at", "")
                    nudge_ok = (not last) or _age_hours(last) >= _RENUDGE_HOURS
                    if age_h >= due_h and nudge_ok:
                        await client.send_message(
                            "me",
                            "⏰ Awaiting your reply\n" + _fmt_item(item),
                            link_preview=False,
                        )
                        item["last_nudge_at"] = now.isoformat()
                        changed = True
                        logger.info("nudged: %s", item["chat_title"])
                local = now.astimezone(tz)
                if (local.hour == settings.watchdog_digest_hour
                        and state.last_digest_date != str(local.date())
                        and state.pending):
                    items = sorted(state.pending.values(),
                                   key=lambda i: i["detected_at"])
                    await client.send_message(
                        "me",
                        f"🌅 Waiting on you ({len(items)}):\n\n"
                        + "\n\n".join(_fmt_item(i) for i in items),
                        link_preview=False,
                    )
                    state.last_digest_date = str(local.date())
                    changed = True
                    logger.info("digest sent (%d items)", len(items))
                if changed:
                    state.save()
            except Exception:  # noqa: BLE001
                logger.exception("nudge_loop iteration failed")
            await asyncio.sleep(_POLL_SECONDS)

    loop_task = asyncio.create_task(nudge_loop())
    try:
        await client.run_until_disconnected()
    finally:
        loop_task.cancel()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
