"""Conversation policy for the autonomous email watch.

Pure, testable helpers plus one notifier, kept out of ``app/email_inbox_watch``
so the rules can be unit-tested with no model, no mailbox and no service.

Two governor decisions (Gary, 2026-10-20) drive this module:

1. "do not waste time on obvious marketing emails and stuff that are not
   genuine interaction" -> ``is_bulk`` / ``is_bulk_by_headers`` run BEFORE any
   model call, so promo mail costs neither a read nor a token.
2. "keep going ... until the point where they request for a face to face
   meeting, at which point reach back here" -> ``MEETING_PATTERN`` detects that
   request so the watch can hold the reply and escalate to Discord instead of
   negotiating a slot by itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bulk / marketing pre-filter (runs before the message is even fetched)
# ---------------------------------------------------------------------------

# Machine mailboxes can never be replied to usefully. This lived in
# email_inbox_watch as the tier-0 send gate, but tier-0 runs AFTER the model
# call - so machine mail still cost a token. Defined here instead, where the
# pre-filter can also use it (single source of truth).
MACHINE_SENDER_PATTERN = re.compile(
    r"(?i)(?:^|[<\s,])(?:no-?reply|do-?not-?reply|donotreply|bounce|"
    r"mailer-daemon|postmaster|notifications?|notify|verify|alerts?)@"
)

BULK_SENDER_PATTERN = re.compile(
    r"(?i)(?:^|[<\s,])(?:"
    r"newsletters?|marketing|promos?|promotions?|offers?|deals?|campaigns?|"
    r"mailer|mailbot|emailmarketing|digest|bulletins?|"
    r"notifications?|notify|alerts?|updates?"
    r")@"
)

BULK_SUBJECT_PATTERN = re.compile(
    r"(?i)(?:"
    r"unsubscribe|% off|\d+\s*%\s*off|flash sale|clearance|"
    r"limited time|last chance|act now|don'?t miss|"
    r"exclusive offer|free shipping|shop now|buy now|"
    r"webinar|newsletter|promotion|"
    r"tiktok shop|verified badge|grow your|boost your|"
    r"your order|order shipped|shipping confirmation|out for delivery|"
    r"receipt for|payment received|your invoice"
    r")"
)


def _addr(sender: str) -> str:
    """Bare email address out of a ``Name <addr>`` header value."""
    match = re.search(r"<([^>]+)>", sender or "")
    return (match.group(1) if match else (sender or "")).strip().lower()


def _denylist() -> tuple[str, ...]:
    raw = os.getenv("EMAIL_WATCH_SENDER_DENYLIST", "")
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def is_bulk(summary_row: dict) -> str | None:
    """Return a short reason if this search hit is bulk/marketing, else ``None``.

    Deliberately conservative in direction: a false positive only means we do
    not reply to a message (it stays unread and is still visible to a human),
    whereas a false negative burns a model call on promo mail.
    """
    sender = (summary_row.get("from") or "").lower()
    subject = summary_row.get("subject") or ""
    addr = _addr(sender)
    if addr and any(token in addr for token in _denylist()):
        return "denylist"
    if MACHINE_SENDER_PATTERN.search(addr or sender):
        return "machine_sender"
    if BULK_SENDER_PATTERN.search(addr or sender):
        return "bulk_sender"
    if BULK_SUBJECT_PATTERN.search(subject):
        return "bulk_subject"
    return None


def is_bulk_by_headers(headers: dict) -> str | None:
    """Return a reason if the fetched headers say bulk/list mail.

    ``List-Unsubscribe`` is the single most reliable bulk signal and it is only
    available after the message is read, so this is the second gate: it still
    saves the model call.
    """
    lowered = {str(k).lower(): (v or "") for k, v in (headers or {}).items()}
    if lowered.get("list-unsubscribe"):
        return "list-unsubscribe"
    if lowered.get("list-id"):
        return "list-id"
    precedence = str(lowered.get("precedence", "")).strip().lower()
    if precedence in {"bulk", "list", "junk"}:
        return "precedence"
    return None


# ---------------------------------------------------------------------------
# Meeting / face-to-face request detection (the governor's escalation trigger)
# ---------------------------------------------------------------------------

MEETING_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:jump|hop|get)\s+on\s+(?:a\s+)?(?:call|zoom|teams|meet)\b|"
    r"\b(?:zoom|google\s+meet|ms\s+teams|webex|facetime)\b|"
    r"\bface[-\s]?to[-\s]?face\b|"
    r"\bin[-\s]person\b|"
    r"\bover\s+the\s+phone\b|"
    r"\b(?:schedule|set\s+up|arrange|book|organi[sz]e)\s+(?:a\s+|an\s+)?"
    r"(?:short\s+|quick\s+|brief\s+)?(?:call|meeting|discussion|chat)\b|"
    r"\b(?:meet|meeting|call)\b[^.\n]{0,40}\b(?:to\s+discuss|with\s+you|"
    r"with\s+your\s+team)\b|"
    r"\b(?:are|would|will|is)\s+(?:you|he|she|we)\s+(?:be\s+)?(?:free|available)\b|"
    r"\b(?:for|have|do)\s+(?:a\s+|an\s+)?(?:quick\s+|short\s+|brief\s+|brief\s+)?"
    r"(?:call|chat|conversation)\b|"
    r"\blet'?s\s+(?:get\s+on|have|schedule|arrange|organi[sz]e)\b|"
    r"\b(?:your|his)\s+availability\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b[^.\n]{0,25}"
    r"\b(?:SGT|GMT|UTC|PST|PDT|EST|EDT|CET|BST|HKT)\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b\s+(?:on|next)\b"
    r")"
)


def meeting_requested(text: str) -> str | None:
    """Return the matched phrase when an inbound body asks for a meeting."""
    match = MEETING_PATTERN.search(text or "")
    return match.group(0).strip() if match else None


# ---------------------------------------------------------------------------
# Per-thread reply cap: two autoresponders must not ping-pong all night
# ---------------------------------------------------------------------------


def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def per_thread_cap() -> int:
    try:
        return int(os.getenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", "2"))
    except ValueError:
        return 2


def thread_reply_allowed(
    state: dict, thread: str | None, now: datetime | None = None
) -> bool:
    """Reserve one automated reply slot for ``thread``; False when exhausted.

    Reserve-on-check: a True return consumes a slot. That is intentional - the
    only call site is the decision to send, and consuming on check means a
    single write, so two concurrent ticks cannot both slip past the cap.
    """
    if not thread:
        return True
    today = _day_key(now)
    counts = state.setdefault("thread_replies", {})
    record = counts.setdefault(thread, {"day": today, "count": 0})
    if record.get("day") != today:
        record["day"] = today
        record["count"] = 0
    if int(record.get("count", 0)) >= per_thread_cap():
        return False
    record["count"] = int(record.get("count", 0)) + 1
    return True


# ---------------------------------------------------------------------------
# Escalation notice -> Discord
# ---------------------------------------------------------------------------

DEFAULT_ESCALATION_CHANNEL = "1560756241070952478"  # #trademark-registration


def escalation_channel() -> str:
    return (
        os.getenv("EMAIL_WATCH_ESCALATION_DISCORD_CHANNEL_ID")
        or DEFAULT_ESCALATION_CHANNEL
    ).strip()


def format_meeting_escalation(
    *,
    sender: str,
    subject: str,
    thread: str | None,
    matched: str,
    snippet: str = "",
) -> str:
    """Render the governor-facing escalation notice (plain text, <=2000 chars)."""
    lines = [
        "**🔔 Meeting request — autonomous reply held**",
        f"**From:** {sender or '(unknown)'}",
        f"**Subject:** {subject or '(none)'}",
        f"**Trigger:** `{matched}`",
    ]
    if thread:
        lines.append(f"**Thread:** `{thread}`")
    if snippet:
        lines.append(f"**They said:** _{snippet.strip()[:400]}_")
    lines.append(
        "\nA holding reply has been drafted and NOT sent. Pick a slot and I will "
        "confirm it and send the invite."
    )
    return "\n".join(lines)


def notify_discord(text: str, channel_id: str | None = None) -> bool:
    """Post ``text`` to Discord. Never raises - a failed notice must not kill a tick."""
    try:
        from .vault import get_vault

        token = get_vault().get_value("DISCORD_BOT_TOKEN")
    except Exception:  # pragma: no cover - vault unavailable in some contexts
        logger.warning("[email-watch] cannot load DISCORD_BOT_TOKEN; skip notice")
        return False
    if not token:
        logger.warning("[email-watch] DISCORD_BOT_TOKEN empty; skip notice")
        return False
    target = channel_id or escalation_channel()
    body = json.dumps({"content": text[:1900]}).encode("utf-8")
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{target}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "truesight-autopilot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("[email-watch] Discord notice failed: %s", exc)
        return False


# ---- matter cards + nudge loop (EMAIL_SEND_POLICY.md S4) -------------------
#
# S4 specifies that any inbound creating an obligation becomes a "matter":
# a draft is held, a card is posted to its routed channel, and a matter with no
# governor answer after ``nudge_after_days`` is nudged once, then keeps showing
# as aging. Only the first half was ever built -- drafts accumulated silently.
# On 2026-10-21 that surfaced as 15+ unsent drafts across the BIONPACT
# trademark threads and correspondents waiting five days for an acknowledgement.
# This closes the loop.

NUDGE_AFTER_DAYS = int(os.getenv("EMAIL_WATCH_NUDGE_AFTER_DAYS", "3"))

# Routing table from S4. Ordered longest-first where prefixes overlap.
ROUTING_TABLE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("trademark", "bionpact", "trade mark", "ipos"), "1560756241070952478"),
    (
        ("freight", "shipment", "container", "customs", "nfe", "du-e"),
        "1560756241070952478",
    ),
    (("invoice", "payment", "bank", "wire", "ocbc"), "1560756241070952478"),
)


def route_for(subject: str, sender: str = "") -> str:
    """Pick the Discord channel for a matter. Falls back to the escalation channel."""
    hay = f"{subject} {sender}".lower()
    for needles, channel in ROUTING_TABLE:
        if any(n in hay for n in needles):
            return channel
    return escalation_channel()


def format_matter_card(
    *,
    subject: str,
    sender: str,
    received: str,
    thread_id: str,
    status: str = "awaiting-decision",
    needs: str = "(not classified)",
    proposed: str = "draft created",
    deadline: str = "none stated",
) -> str:
    """Render the S4 matter-card format."""
    return (
        f"**Matter:** {subject}\n"
        f"**From:** {sender} · **Received:** {received} · **Ref:** `{thread_id}`\n"
        f"**Deadline:** {deadline}\n"
        f"**Status:** {status}\n"
        f"**Needs from Gary:** {needs}\n"
        f"**Proposed reply:** {proposed}\n"
        f"**Email thread:** `{thread_id}`"
    )


def due_for_nudge(record: dict, now: datetime | None = None) -> bool:
    """True when a matter has sat unanswered past NUDGE_AFTER_DAYS and is not yet nudged."""
    if not isinstance(record, dict):
        return False
    if record.get("status") != "awaiting-decision":
        return False
    if record.get("nudged_at"):
        return False
    at = record.get("at") or ""
    if not at:
        return False
    try:
        opened = datetime.fromisoformat(at)
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return (current - opened).days >= NUDGE_AFTER_DAYS


def format_nudge(card: dict, days: int) -> str:
    """Nudge text for an aging matter."""
    return (
        f"⏰ **Still unanswered ({days}d):** {card.get('subject', '(no subject)')}\n"
        f"**From:** {card.get('from', '?')} · **Thread:** `{card.get('thread', '?')}`\n"
        f"A draft is held in Gmail awaiting a decision. Nothing has been sent."
    )
