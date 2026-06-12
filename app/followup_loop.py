"""
Durable follow-up comb loop — hourly background runner.

Checks open follow-ups whose next_check is due, runs the appropriate
probe, and on strike spins a full Sophia turn in the originating thread.

Started alongside email_poller and aws_monitor in the main lifespan.

"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.followup_probes import run_probe
from app.followups import (
    get_state,
    list_open,
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


def _build_strike_message(followup: dict[str, Any], probe_result: dict[str, Any]) -> str:
    """Build a message to post in the thread when a follow-up strikes."""
    title = followup.get("title", "Untitled follow-up")
    condition = followup.get("condition", {})
    kind = condition.get("kind", "unknown")
    evidence = probe_result.get("evidence", "")

    lines = [
        f"🔔 **Follow-up triggered: {title}**",
        f"",
        f"Condition: {kind}",
        f"Evidence: {evidence}",
        f"",
        f"Processing this now — I'll report back in this thread.",
    ]
    return "\n".join(lines)


def _build_escalation_message(followup: dict[str, Any]) -> str:
    """Build a ping message for escalation (time passed, no strike yet)."""
    title = followup.get("title", "Untitled follow-up")
    schedule = followup.get("schedule", {})
    escalate_after = schedule.get("escalate_after_days", 1)
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

    schedule = followup.get("schedule", {})
    escalate_after = schedule.get("escalate_after_days", 1)
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

    if probe_result.get("struck"):
        # Condition struck — spin a Sophia turn in the thread
        logger.info(
            "Follow-up %s STRUCK! Spinning turn in thread %s",
            followup_id, thread_id,
        )

        if thread_id:
            message = _build_strike_message(followup, probe_result)
            await _post_to_thread(chat_id, thread_id, message)

            # Spin a full Sophia turn
            await _spin_sophia_turn(chat_id, thread_id, followup, probe_result)

        # Close the follow-up as resolved
        set_status(followup_id, "resolved")
        logger.info("Follow-up %s resolved (condition struck)", followup_id)

    elif days_elapsed >= escalate_after:
        # Escalation day reached but no strike — ping thread once
        last_pinged = state.get("last_pinged")
        if not last_pinged:
            logger.info(
                "Follow-up %s escalation day reached, pinging thread %s",
                followup_id, thread_id,
            )
            if thread_id:
                message = _build_escalation_message(followup)
                await _post_to_thread(chat_id, thread_id, message)

            upsert_state(followup_id, last_pinged=_now_iso())
        else:
            logger.debug(
                "Follow-up %s already pinged at %s, skipping",
                followup_id, last_pinged,
            )

    # Update scheduling state
    upsert_state(
        followup_id,
        last_checked=_now_iso(),
        next_check=_compute_next_check(followup),
        attempts=attempts,
    )


# ── thread communication ──────────────────────────────────────────────────


async def _post_to_thread(chat_id: str | None, thread_id: str, message: str):
    """Post a message to a Telegram thread.

    Uses the Telegram adapter's send_message if available, otherwise
    falls back to a direct HTTP call.
    """
    try:
        # Try to use the Telegram adapter
        from app.telegram_adapter import send_telegram_message

        await send_telegram_message(
            chat_id=chat_id or "-1003919341801",
            text=message,
            message_thread_id=int(thread_id) if thread_id.isdigit() else None,
        )
        logger.info("Posted to thread %s: %s", thread_id, message[:80])
    except ImportError:
        # Fallback: direct HTTP call
        logger.warning("Telegram adapter not available, using direct HTTP")
        await _post_to_thread_direct(chat_id, thread_id, message)
    except Exception as e:
        logger.error("Failed to post to thread %s: %s", thread_id, e)


async def _post_to_thread_direct(chat_id: str | None, thread_id: str, message: str):
    """Direct HTTP fallback for posting to Telegram thread."""
    import httpx

    from app.config import settings

    bot_token = settings.telegram_bot_token
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot post to thread")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id or "-1003919341801",
        "text": message,
        "message_thread_id": int(thread_id) if thread_id.isdigit() else None,
        "parse_mode": "Markdown",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            logger.error(
                "Telegram API error: %s %s",
                resp.status_code, resp.text[:200],
            )


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
        from app.main import process_message

        # Build a context message for Sophia
        context_message = (
            f"[FOLLOW-UP TRIGGERED]\n\n"
            f"Follow-up: {followup.get('title', 'Untitled')}\n"
            f"ID: {followup.get('id', 'unknown')}\n"
            f"Condition: {followup.get('condition', {}).get('kind', 'unknown')}\n"
            f"Evidence: {probe_result.get('evidence', '')}\n\n"
            f"Process this follow-up and report back in this thread."
        )

        # Create a session_id for the turn
        session_id = f"tg:{chat_id or '-1003919341801'}:{thread_id}"

        # Process through the normal message path
        await process_message(
            session_id=session_id,
            message=context_message,
            chat_id=chat_id or "-1003919341801",
            thread_id=int(thread_id) if thread_id.isdigit() else None,
        )
        logger.info("Sophia turn spun for follow-up %s in thread %s", followup.get("id"), thread_id)

    except ImportError:
        logger.warning("process_message not available — cannot spin Sophia turn")
    except Exception as e:
        logger.exception("Failed to spin Sophia turn: %s", e)
