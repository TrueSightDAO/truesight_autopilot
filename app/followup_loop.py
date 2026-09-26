"""
Durable follow-up comb loop — hourly background runner.

Checks open follow-ups whose next_check is due, runs the appropriate
probe, and on strike spins a full Sophia turn in the originating thread.

Started alongside email_poller and aws_monitor in the main lifespan.

"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any

from app.followup_probes import get_escalate_after_days, run_probe
from app.followups import (
    get_state,
    next_due,
    set_status,
    upsert_state,
)

logger = logging.getLogger("autopilot.followups.loop")


# ── helpers ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_next_check(followup: dict[str, Any]) -> str:
    """Compute the next check time based on the schedule."""
    from datetime import timedelta

    schedule = followup.get("schedule", {})
    check = schedule.get("check", "daily")
    now = datetime.now(timezone.utc)

    if check == "weekly":
        next_time = now + timedelta(days=7)
    else:
        next_time = now + timedelta(hours=1)  # daily = check every hour

    return next_time.isoformat()


def _retry_soon_iso() -> str:
    """Next check in ~1 hour.

    Used when a strike failed to notify or failed to dispatch its turn, so the
    item retries on the *very next* pass instead of waiting a full schedule
    interval (weekly = 7 days) before it can fire again.
    """
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _get_thread_id(followup: dict[str, Any]) -> str | None:
    """Extract thread_id from a follow-up, handling both int and str."""
    tid = followup.get("thread_id")
    if tid is None:
        return None
    return str(tid)


def _get_chat_id(followup: dict[str, Any]) -> str | None:
    """Extract chat_id from a follow-up."""
    cid = followup.get("chat_id")
    if cid is None:
        return None
    return str(cid)


def _build_strike_message(
    followup: dict[str, Any], probe_result: dict[str, Any]
) -> str:
    """Build a message to post in the thread when a follow-up strikes."""
    title = followup.get("title", "Untitled follow-up")
    condition = followup.get("condition", {})
    kind = condition.get("kind", "unknown")
    evidence = probe_result.get("evidence", "")

    lines = [
        f"🔔 **Follow-up triggered: {title}**",
        "",
        f"Condition: {kind}",
        f"Evidence: {evidence}",
        "",
        "Processing this now — I'll report back in this thread.",
    ]
    return "\n".join(lines)


def _build_escalation_message(followup: dict[str, Any]) -> str:
    """Build a ping message for escalation (time passed, no strike yet)."""
    title = followup.get("title", "Untitled follow-up")
    escalate_after = get_escalate_after_days(followup)
    created_at = followup.get("created_at", "unknown")

    return (
        f"⏰ **Follow-up reminder: {title}**\n\n"
        f"Created {created_at} (escalation after {escalate_after} day(s)).\n"
        f"No condition has struck yet — this is a scheduled check-in.\n"
        f"Gary, any updates on this?"
    )


# ── the loop ──────────────────────────────────────────────────────────────


async def followup_loop(interval_seconds: int = 3600):
    """
    Background loop that checks open follow-ups every hour.

    For each due follow-up:
    1. Run the probe
    2. If struck → spin a Sophia turn in the thread
    3. If escalation day with no strike → ping thread once
    4. Update sidecar state (next_check, attempts, last_checked)

    Args:
        interval_seconds: How often to run the comb (default 3600 = 1 hour)
    """
    logger.info("Follow-up loop started (interval=%ss)", interval_seconds)

    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception("Follow-up loop tick failed: %s", e)

        await asyncio.sleep(interval_seconds)


async def _tick():
    """Single tick of the follow-up loop."""
    now = datetime.now(timezone.utc)
    due = next_due(now)

    if not due:
        logger.debug("Follow-up loop: no due follow-ups")
        return

    logger.info("Follow-up loop: %d follow-up(s) due", len(due))

    for followup in due:
        followup_id = followup.get("id", "unknown")
        logger.info("Processing follow-up: %s", followup_id)

        try:
            await _process_one(followup, now)
        except Exception as e:
            logger.exception("Failed to process follow-up %s: %s", followup_id, e)
            # Still update state so we don't retry immediately
            upsert_state(
                followup_id,
                last_checked=_now_iso(),
                next_check=_compute_next_check(followup),
                attempts=(get_state(followup_id) or {}).get("attempts", 0) + 1,
            )


async def _process_one(followup: dict[str, Any], now: datetime):
    """Process a single due follow-up."""
    followup_id = followup.get("id", "unknown")
    state = get_state(followup_id) or {}
    attempts = state.get("attempts", 0) + 1

    # Run the probe
    probe_result = run_probe(followup, now)
    logger.info(
        "Follow-up %s probe result: struck=%s, evidence=%s",
        followup_id,
        probe_result.get("struck"),
        probe_result.get("evidence", "")[:100],
    )

    escalate_after = get_escalate_after_days(followup)
    created_at_str = followup.get("created_at", "")

    # Calculate days elapsed
    days_elapsed = 0
    if created_at_str:
        try:
            created = datetime.fromisoformat(created_at_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_elapsed = (now - created).total_seconds() / 86400
        except (ValueError, TypeError):
            pass

    thread_id = _get_thread_id(followup)
    chat_id = _get_chat_id(followup)

    strike_retry = False

    if probe_result.get("struck"):
        # Condition struck — notify the thread AND spin a Sophia turn.
        logger.info(
            "Follow-up %s STRUCK! Spinning turn in thread %s",
            followup_id,
            thread_id,
        )

        notified = False
        turned = False
        if thread_id:
            message = _build_strike_message(followup, probe_result)
            notified = await _post_to_thread(chat_id, thread_id, message)
            turned = await _spin_sophia_turn(chat_id, thread_id, followup, probe_result)

        # Only resolve once BOTH the notification AND the spun turn actually
        # landed. This previously ran unconditionally, so a strike that failed
        # to notify (bad symbol + 400) AND failed to spin (bad symbol) still got
        # marked "resolved" forever — silently dropping 9 real follow-ups
        # since #438. On failure we leave it OPEN + retry soon.
        if notified and turned:
            set_status(followup_id, "resolved")
            logger.info(
                "Follow-up %s resolved (struck; notified + turn ran)", followup_id
            )
        else:
            strike_retry = True
            logger.error(
                "Follow-up %s STRUCK but notify=%s turn=%s — leaving OPEN to "
                "retry next pass (NOT resolving)",
                followup_id,
                notified,
                turned,
            )

    elif days_elapsed >= escalate_after:
        # Escalation day reached but no strike — ping thread once
        last_pinged = state.get("last_pinged")
        if not last_pinged:
            logger.info(
                "Follow-up %s escalation day reached, pinging thread %s",
                followup_id,
                thread_id,
            )
            if thread_id:
                message = _build_escalation_message(followup)
                await _post_to_thread(chat_id, thread_id, message)

            upsert_state(followup_id, last_pinged=_now_iso())
        else:
            logger.debug(
                "Follow-up %s already pinged at %s, skipping",
                followup_id,
                last_pinged,
            )

    # Update scheduling state. A strike that failed to notify/dispatch is
    # retried on the next hourly pass instead of a whole schedule interval.
    next_check = _retry_soon_iso() if strike_retry else _compute_next_check(followup)
    upsert_state(
        followup_id,
        last_checked=_now_iso(),
        next_check=next_check,
        attempts=attempts,
    )


# ── thread communication ──────────────────────────────────────────────────


async def _post_to_thread(chat_id: str | None, thread_id: str, message: str) -> bool:
    """Post a message to a Telegram thread. Returns True iff it was delivered.

    Uses the Telegram adapter's REAL ``send_message`` symbol. The previous
    ``send_telegram_message`` import never existed, so this silently
    ImportError'd into a hand-rolled HTTP path that 400'd with "message thread
    not found" (#438) — every strike notification was dropped, then the item
    was marked resolved anyway (see :func:`_process_one`).
    """
    try:
        from app.telegram_adapter import send_message
    except ImportError:
        # Fallback: direct HTTP call
        logger.warning("Telegram adapter not available, using direct HTTP")
        return await _post_to_thread_direct(chat_id, thread_id, message)

    try:
        resolved_chat = int(chat_id) if chat_id else -1003919341801
    except (TypeError, ValueError):
        resolved_chat = -1003919341801
    tid = int(thread_id) if str(thread_id).isdigit() else None

    try:
        # send_message is synchronous (httpx.post + retry/backoff); run it off
        # the event loop so a strike can never block the comb loop.
        msg_id = await asyncio.to_thread(
            send_message, resolved_chat, message, tid, require_thread=True
        )
    except Exception as e:
        logger.error("Failed to post to thread %s: %s", thread_id, e)
        return False

    if msg_id is None:
        logger.error("sendMessage to thread %s returned no message_id", thread_id)
        return False
    logger.info("Posted to thread %s: %s", thread_id, message[:80])
    return True


async def _post_to_thread_direct(
    chat_id: str | None, thread_id: str, message: str
) -> bool:
    """Direct HTTP fallback for posting to Telegram thread.

    Renders Markdown → Telegram HTML (same as the adapter path) and sends with
    parse_mode=HTML. Legacy "Markdown" parse mode doesn't support the ``**bold**``
    syntax these messages use, which caused Telegram 400 "can't parse entities".
    """
    import httpx

    from app.config import settings
    from app.telegram_adapter import markdown_to_telegram_html

    bot_token = settings.telegram_bot_api_key
    if not bot_token:
        logger.error("TELEGRAM_BOT_API_KEY not set — cannot post to thread")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id or "-1003919341801",
        "text": markdown_to_telegram_html(message),
        "message_thread_id": int(thread_id) if thread_id.isdigit() else None,
        "parse_mode": "HTML",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            return True
        logger.error(
            "Telegram API error: %s %s",
            resp.status_code,
            resp.text[:200],
        )
        # Fallback: retry as **escaped** plain text. The previous version kept
        # parse_mode=HTML while swapping in the raw markdown (unescaped '&', '<',
        # '**'), so the retry 400'd with "can't parse entities" and the ping was
        # dropped. Escape the entities, and only drop message_thread_id when the
        # topic itself is gone ("message thread not found") — otherwise keep it so
        # the notification still lands IN the topic.
        thread_missing = "thread not found" in (resp.text or "").lower()
        payload.pop("parse_mode", None)
        payload["text"] = html.escape(message, quote=False)
        if thread_missing:
            payload.pop("message_thread_id", None)
        resp2 = await client.post(url, json=payload, timeout=30)
        if resp2.status_code == 200:
            if thread_missing:
                # Landed in the group, NOT the requested topic — report
                # undelivered so the caller retries instead of resolving.
                logger.error(
                    "Telegram fallback landed outside thread %s (topic missing)",
                    thread_id,
                )
                return False
            return True
        logger.error(
            "Telegram API error (fallback): %s %s",
            resp2.status_code,
            resp2.text[:200],
        )
        return False


async def _spin_sophia_turn(
    chat_id: str | None,
    thread_id: str,
    followup: dict[str, Any],
    probe_result: dict[str, Any],
):
    """Spin a full Sophia turn in the thread.

    Builds a seed message with follow-up context + probe evidence,
    then runs it through the per-topic-locked executor path so it
    serializes with any live governor message.
    """
    try:
        from app.main import _chat_blocking_turn, _session_lock
        from app.telegram_adapter import build_session_id, resolve_governor_public_key
    except ImportError:
        logger.warning("Turn dispatcher unavailable — cannot spin Sophia turn")
        return False

    public_key = resolve_governor_public_key()
    if not public_key:
        logger.error("Cannot resolve governor public key — aborting turn spin")
        return False

    try:
        resolved_chat = int(chat_id) if chat_id else -1003919341801
    except (TypeError, ValueError):
        resolved_chat = -1003919341801
    tid = int(thread_id) if str(thread_id).isdigit() else None

    context_message = (
        f"[FOLLOW-UP TRIGGERED]\n\n"
        f"Follow-up: {followup.get('title', 'Untitled')}\n"
        f"ID: {followup.get('id', 'unknown')}\n"
        f"Condition: {followup.get('condition', {}).get('kind', 'unknown')}\n"
        f"Evidence: {probe_result.get('evidence', '')}\n\n"
        f"Process this follow-up and report back in this thread."
    )

    # Rebuild the session id EXACTLY as telegram_adapter does, so the spun turn
    # loads the thread's real transcript. The old bare ``tg:<chat>:<thread>``
    # key hashed to a different session file, so even a working dispatch would
    # have landed in an empty, role-less session.
    session_id = f"{public_key[:20]}:{build_session_id(resolved_chat, tid)}"
    try:
        # Same per-session lock chat_blocking uses, so a loop-spun turn and a
        # live governor message in the same thread serialize instead of racing.
        async with _session_lock(session_id):
            await _chat_blocking_turn(session_id, context_message, public_key)
    except Exception as e:
        logger.exception("Failed to spin Sophia turn: %s", e)
        return False

    logger.info(
        "Sophia turn spun for follow-up %s in thread %s",
        followup.get("id"),
        thread_id,
    )
    return True
