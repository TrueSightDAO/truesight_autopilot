"""Calendar watchdog — Sophia's daily self-check of the admin@truesight.me calendar.

Born from a governor ask (Gary, 2026-10-10): "you need a daily mechanism to
self check the calendar" — a calendar entry alone does not wake the autopilot,
so the *intention* needs an actual **runner**. This is that runner, modelled on
the existing background loops (``followup_loop`` hourly comb; ``attention_watchdog``
daily digest):

  * Every ``CALENDAR_WATCH_INTERVAL_HOURS`` (default 24 h, evaluated on the loop)
    it pulls the next ``CALENDAR_WATCH_DAYS_AHEAD`` (default 7) days of events
    from the **admin account** (admin@truesight.me) via the shared token store.
  * It condenses each event into the attention facts the autopilot needs:
      - events with **un-responded attendees** (``needsAction``) that are near,
      - events happening **soon** (today/tomorrow) that may need prep,
  * and posts a **plain-text digest into a configured Telegram topic** so a
    governor can act on it.

Scope is deliberately read-only + advisory (same posture as the attention
watchdog): it **never** mutates a calendar and **never** DMs anyone but its own
configured topic. All outbound sending is additionally gated behind
``CALENDAR_WATCH_ENABLE_POSTS`` (default off) for a dry-run-first rollout, and
the whole loop is off unless ``CALENDAR_WATCH_ENABLED=true``.

Runs inside the FastAPI lifespan alongside the other loops (main.py), so it
needs no separate systemd unit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings

logger = logging.getLogger("autopilot.calendar.watchdog")

_TERMINAL = {"accepted", "declined", "tentative"}


def _parse_dt(value: str | None) -> datetime | None:
    """Best-effort ISO parse into an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    if "T" not in text:  # all-day date
        text = f"{text}T00:00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _needs_attention(ev: dict, now: datetime) -> list[str]:
    """Return a list of human-readable attention reasons for an event (pure).

    Empty list ⇒ the event needs nothing from the autopilot.
    """
    reasons: list[str] = []
    start = _parse_dt(ev.get("start"))
    if start is not None:
        hours = (start - now).total_seconds() / 3600
        if 0 <= hours <= 48:
            reasons.append(f"starts in {hours:.0f}h — confirm prep")

    pending = [
        a
        for a in (ev.get("attendees") or [])
        if (a.get("status") or "needsAction") not in _TERMINAL
    ]
    if pending and start is not None and (start - now).total_seconds() <= 7 * 86400:
        names = ", ".join(a.get("email", "?") for a in pending)
        reasons.append(f"awaiting response: {names}")
    return reasons


def build_digest(events: list[dict], now: datetime | None = None) -> str:
    """Render a digest message for the given events (pure; returns '' if none)."""
    now = now or datetime.now(timezone.utc)
    rows: list[str] = []
    for ev in events:
        reasons = _needs_attention(ev, now)
        if not reasons:
            continue
        when = ev.get("start") or "(no time)"
        rows.append(f"• {when} — {ev.get('summary')}\n  ↳ " + "; ".join(reasons))
    if not rows:
        return ""
    header = f"🗓 **Calendar self-check** — {len(rows)} event(s) need attention"
    return "\n".join([header, ""] + rows)


async def calendar_watchdog_loop(interval_seconds: int | None = None):
    """Background loop; runs until cancelled."""
    from .tools.google_calendar import fetch_upcoming_events

    interval = int(
        interval_seconds
        or max(1, int(getattr(settings, "calendar_watch_interval_hours", 24))) * 3600
    )
    days_ahead = int(getattr(settings, "calendar_watch_days_ahead", 7))
    logger.info(
        "Calendar watchdog started (interval=%ss, days_ahead=%s)", interval, days_ahead
    )

    while True:
        try:
            events = await asyncio.to_thread(
                fetch_upcoming_events,
                getattr(settings, "calendar_watch_account", "admin"),
                days_ahead,
                100,
            )
            digest = build_digest(events)
            if digest:
                await _post_digest(digest)
                await _post_digest_discord(digest)
            else:
                logger.info("Calendar watchdog: nothing needs attention")
            if getattr(settings, "calendar_watch_discord_native_events", False):
                try:
                    created = await asyncio.to_thread(sync_native_events, events)
                    if created:
                        logger.info(
                            "Calendar watchdog: mirrored %d native Discord event(s)",
                            created,
                        )
                except Exception as e:  # never kill the loop
                    logger.exception("Calendar watchdog: native sync failed: %s", e)
        except Exception as e:  # never kill the loop
            logger.exception("Calendar watchdog tick failed: %s", e)

        await asyncio.sleep(interval)


async def _post_digest(digest: str) -> bool:
    """Post the digest to the configured topic (honouring the send gate)."""
    thread_id = str(getattr(settings, "calendar_watch_thread_id", "") or "").strip()
    if not thread_id:
        logger.info("Calendar watchdog: no CALENDAR_WATCH_THREAD_ID set — logging only")
        logger.info("Calendar digest (undelivered):\n%s", digest)
        return False
    if not getattr(settings, "calendar_watch_enable_posts", False):
        logger.info(
            "Calendar watchdog: CALENDAR_WATCH_ENABLE_POSTS=false — would post to "
            "thread %s:\n%s",
            thread_id,
            digest,
        )
        return False

    try:
        from app.telegram_adapter import send_message
    except ImportError:  # pragma: no cover
        logger.warning("Telegram adapter unavailable — cannot post calendar digest")
        return False

    chat_id = getattr(settings, "telegram_home_group_id", "") or "-1003919341801"
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        chat = -1003919341801
    tid = int(thread_id) if thread_id.isdigit() else None
    try:
        msg_id = await asyncio.to_thread(send_message, chat, digest, tid, True)
    except Exception as e:
        logger.error("Calendar watchdog: post failed: %s", e)
        return False
    logger.info("Calendar watchdog: posted digest (msg_id=%s)", msg_id)
    return msg_id is not None


# ── Discord delivery: text digest + native scheduled events ──────────────────

_NATIVE_STATE_FILE = "calendar_native_events.json"


def _native_state_path() -> Path:
    base = Path(getattr(settings, "session_log_dir", "/tmp/autopilot_sessions"))
    base.mkdir(parents=True, exist_ok=True)
    return base / _NATIVE_STATE_FILE


def _load_native_state() -> dict:
    try:
        return json.loads(_native_state_path().read_text()) or {}
    except Exception:  # noqa: BLE001 -- missing/corrupt state must not raise
        return {}


def _save_native_state(state: dict) -> None:
    try:
        _native_state_path().write_text(json.dumps(state))
    except Exception:  # noqa: BLE001
        logger.warning("Calendar watchdog: could not persist native-event state")


def _native_event_payload(ev: dict) -> dict | None:
    """Map a condensed calendar event to a native Discord event payload (pure).

    Returns None when there is no usable start. All-day events get a 1-hour
    block. Times are normalised to UTC ISO-8601 (Discord requires tz-aware).
    """
    start = _parse_dt(ev.get("start"))
    if start is None:
        return None
    end = _parse_dt(ev.get("end"))
    if end is None or end <= start:
        end = start + timedelta(hours=1)
    return {
        "name": (ev.get("summary") or "(event)")[:100],
        "start_iso": start.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "end_iso": end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "location": (ev.get("location") or "See calendar")[:100],
        "description": (ev.get("htmlLink") or "")[:1000],
    }


def sync_native_events(events: list[dict]) -> int:
    """Mirror attention-worthy events as native Discord scheduled events.

    Idempotent: a small JSON state file maps calendar event id -> Discord event
    id so repeated ticks do not create duplicates. Returns the number created.
    """
    try:
        from app.discord_adapter import create_scheduled_event
    except ImportError:  # pragma: no cover
        logger.warning("Discord adapter unavailable — cannot sync native events")
        return 0
    now = datetime.now(timezone.utc)
    state = _load_native_state()
    created = 0
    for ev in events:
        eid = str(ev.get("id") or "").strip()
        if not eid or eid in state:
            continue
        if not _needs_attention(ev, now):
            continue
        payload = _native_event_payload(ev)
        if not payload:
            continue
        res = create_scheduled_event(**payload)
        if res and res.get("id"):
            state[eid] = res["id"]
            created += 1
    if created:
        _save_native_state(state)
    return created


async def _post_digest_discord(digest: str) -> bool:
    """Mirror the digest into the configured Discord channel (send-gated)."""
    channel_id = str(
        getattr(settings, "calendar_watch_discord_channel_id", "") or ""
    ).strip()
    if not channel_id:
        return False
    if not getattr(settings, "calendar_watch_enable_posts", False):
        logger.info(
            "Calendar watchdog: CALENDAR_WATCH_ENABLE_POSTS=false — would post to "
            "Discord channel %s",
            channel_id,
        )
        return False
    try:
        from app.discord_adapter import send_message as discord_send
    except ImportError:  # pragma: no cover
        logger.warning("Discord adapter unavailable — cannot post calendar digest")
        return False
    try:
        ids = await asyncio.to_thread(discord_send, channel_id, digest)
    except Exception as e:  # noqa: BLE001
        logger.error("Calendar watchdog: Discord post failed: %s", e)
        return False
    logger.info("Calendar watchdog: posted Discord digest (ids=%s)", ids)
    return bool(ids)
