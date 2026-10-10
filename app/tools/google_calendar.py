"""Google Calendar tools for the autopilot agent — ``calendar.events`` scope.

Reuses the **Gmail OAuth token store** (``config/gmail/{account}_token.json``):
the same per-account token files now carry ``calendar.events`` (granted
2026-10-10 via ``scripts/gmail_add_calendar_scope.py``), so no separate
credential store or OAuth flow is needed. Account resolution mirrors
:mod:`app.tools.gmail_tools` exactly.

Exposed operations:

- ``calendar_list_events(account, days_ahead, max_results, calendar_id)``
- ``calendar_create_event(account, summary, start, end, attendees, description,
  location, recurrence, timezone, send_updates)``
- ``calendar_update_event(account, event_id, ...)`` — PATCH
- ``calendar_delete_event(account, event_id, send_updates)``

Every function returns a JSON string. A missing token or absent Google client
libraries degrade to ``{"status": "error", ...}`` rather than raising, so the
service still boots when Calendar hasn't been provisioned.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .gmail_tools import _resolve_account, _token_data

logger = logging.getLogger("autopilot.tools.calendar")

# The token files carry this scope once re-consented; used as a fallback when
# the stored token omits an explicit scope list.
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_TIMEZONE = "America/Los_Angeles"
_MAX_RESULTS = 250


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _build_service(account: str | None):
    """Return ``(service, error_json_or_None)`` for the Calendar v3 API."""
    name = _resolve_account(account)
    raw = _token_data(name)
    if raw is None:
        return None, _err("calendar credentials missing", account=name)
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:  # pragma: no cover
        return None, _err(f"google client libs unavailable: {e}")

    try:
        creds = Credentials(
            token=raw.get("token"),
            refresh_token=raw.get("refresh_token"),
            token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=raw.get("client_id"),
            client_secret=raw.get("client_secret"),
            scopes=raw.get("scopes") or CALENDAR_SCOPES,
        )
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return service, None
    except Exception as e:
        return None, _err(f"Calendar client init failed: {e}", account=name)


# ── pure helpers (unit-tested) ────────────────────────────────────────────────


def _normalize_recurrence(recurrence: Any) -> list[str] | None:
    """Coerce ``recurrence`` into a Calendar RRULE list, or ``None``.

    Accepts a bare ``"RRULE:FREQ=MONTHLY..."`` string, a bare ``"FREQ=..."``
    body (``RRULE:`` is prepended), or a list of either.
    """
    if not recurrence:
        return None
    items = recurrence if isinstance(recurrence, (list, tuple)) else [recurrence]
    out: list[str] = []
    for item in items:
        if not item:
            continue
        text = str(item).strip()
        if not text:
            continue
        if not text.upper().startswith(("RRULE:", "EXRULE:", "RDATE:", "EXDATE:")):
            text = f"RRULE:{text}"
        out.append(text)
    return out or None


def _normalize_attendees(attendees: Any) -> list[dict[str, str]] | None:
    """Coerce ``attendees`` into Calendar attendee objects.

    Accepts a list of email strings or a list of ``{"email": ...}`` dicts.
    """
    if not attendees:
        return None
    out: list[dict[str, str]] = []
    for a in attendees:
        if isinstance(a, dict):
            email = (a.get("email") or "").strip()
            if not email:
                continue
            entry = {"email": email}
            if a.get("displayName"):
                entry["displayName"] = str(a["displayName"])
            out.append(entry)
        else:
            email = str(a).strip()
            if email:
                out.append({"email": email})
    return out or None


def _time_block(value: str | None, tz: str | None) -> dict[str, str] | None:
    """Build a Calendar ``start``/``end`` block from an ISO date or datetime."""
    if not value:
        return None
    text = str(value).strip()
    if "T" in text:  # a datetime
        block = {"dateTime": text}
        if tz:
            block["timeZone"] = tz
        return block
    return {"date": text}  # an all-day date


def _event_summary(ev: dict[str, Any]) -> dict[str, Any]:
    """Condense an API event into the fields the autopilot cares about."""
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    attendees = ev.get("attendees") or []
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary") or "(no title)",
        "location": ev.get("location") or "",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "htmlLink": ev.get("htmlLink"),
        "recurrence": ev.get("recurrence"),
        "attendees": [
            {
                "email": (a.get("email") or "").lower(),
                "displayName": a.get("displayName") or "",
                "status": a.get("responseStatus") or "needsAction",
            }
            for a in attendees
        ],
    }


# ── operations ────────────────────────────────────────────────────────────────


def fetch_upcoming_events(
    account: str | None = None,
    days_ahead: int = 7,
    max_results: int = 50,
    calendar_id: str = DEFAULT_CALENDAR_ID,
) -> list[dict[str, Any]]:
    """Return condensed upcoming events (list) — used by the watchdog.

    Returns ``[]`` on any error (never raises).
    """
    service, err = _build_service(account)
    if service is None:
        logger.warning("fetch_upcoming_events: %s", err)
        return []
    days = max(1, min(int(days_ahead or 7), 60))
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()
    try:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id or DEFAULT_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max(1, min(int(max_results or 50), _MAX_RESULTS)),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as e:
        logger.warning("calendar events.list failed: %s", e)
        return []
    return [_event_summary(ev) for ev in (resp.get("items") or [])]


def calendar_list_events(
    account: str | None = None,
    days_ahead: int = 7,
    max_results: int = 50,
    calendar_id: str = DEFAULT_CALENDAR_ID,
) -> str:
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    events = fetch_upcoming_events(account, days_ahead, max_results, calendar_id)
    return json.dumps(
        {"status": "ok", "account": _resolve_account(account), "events": events},
        indent=2,
    )


def calendar_create_event(
    account: str | None = None,
    summary: str = "",
    start: str = "",
    end: str = "",
    attendees: Any = None,
    description: str = "",
    location: str = "",
    recurrence: Any = None,
    timezone: str | None = DEFAULT_TIMEZONE,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    send_updates: str = "all",
) -> str:
    if not summary:
        return _err("summary is required")
    start_block = _time_block(start, timezone)
    end_block = _time_block(end, timezone)
    if not start_block or not end_block:
        return _err("start and end are required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]

    body: dict[str, Any] = {
        "summary": summary,
        "start": start_block,
        "end": end_block,
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    norm_rec = _normalize_recurrence(recurrence)
    if norm_rec:
        body["recurrence"] = norm_rec
    norm_att = _normalize_attendees(attendees)
    if norm_att:
        body["attendees"] = norm_att
    try:
        ev = (
            service.events()
            .insert(
                calendarId=calendar_id or DEFAULT_CALENDAR_ID,
                body=body,
                sendUpdates=send_updates or "none",
            )
            .execute()
        )
    except Exception as e:
        return _err(f"events.insert failed: {e}")
    return json.dumps(
        {"status": "ok", "id": ev.get("id"), "htmlLink": ev.get("htmlLink")},
        indent=2,
    )


def calendar_update_event(
    account: str | None = None,
    event_id: str = "",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    attendees: Any = None,
    description: str | None = None,
    location: str | None = None,
    recurrence: Any = None,
    timezone: str | None = DEFAULT_TIMEZONE,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    send_updates: str = "all",
) -> str:
    if not event_id:
        return _err("event_id is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    patch: dict[str, Any] = {}
    if summary is not None:
        patch["summary"] = summary
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start:
        block = _time_block(start, timezone)
        if block:
            patch["start"] = block
    if end:
        block = _time_block(end, timezone)
        if block:
            patch["end"] = block
    norm_rec = _normalize_recurrence(recurrence)
    if norm_rec:
        patch["recurrence"] = norm_rec
    norm_att = _normalize_attendees(attendees)
    if norm_att:
        patch["attendees"] = norm_att
    if not patch:
        return _err("nothing to update")
    try:
        ev = (
            service.events()
            .patch(
                calendarId=calendar_id or DEFAULT_CALENDAR_ID,
                eventId=event_id,
                body=patch,
                sendUpdates=send_updates or "none",
            )
            .execute()
        )
    except Exception as e:
        return _err(f"events.patch failed: {e}")
    return json.dumps(
        {"status": "ok", "id": ev.get("id"), "htmlLink": ev.get("htmlLink")},
        indent=2,
    )


def calendar_delete_event(
    account: str | None = None,
    event_id: str = "",
    calendar_id: str = DEFAULT_CALENDAR_ID,
    send_updates: str = "all",
) -> str:
    if not event_id:
        return _err("event_id is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        service.events().delete(
            calendarId=calendar_id or DEFAULT_CALENDAR_ID,
            eventId=event_id,
            sendUpdates=send_updates or "none",
        ).execute()
    except Exception as e:
        return _err(f"events.delete failed: {e}")
    return json.dumps({"status": "ok", "deleted": event_id}, indent=2)


# ── tool registry ─────────────────────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

_ACCOUNT_PROP = {
    "type": "string",
    "description": "Mailbox/calendar account: 'admin' (default) or 'gary'.",
}
_SEND_UPDATES_PROP = {
    "type": "string",
    "description": "Who to email about the change: 'all' (default), 'externalOnly', 'none'.",
}

TOOL_SPECS = [
    ToolSpec(
        name="calendar_list_events",
        description=(
            "List upcoming Google Calendar events for an account (default "
            "'admin' = admin@truesight.me). Returns id/title/start/attendee "
            "response status. Read-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": _ACCOUNT_PROP,
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look (default 7).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events (default 50).",
                },
            },
        },
        handler=lambda args, ctx: calendar_list_events(
            account=args.get("account"),
            days_ahead=int(args.get("days_ahead") or 7),
            max_results=int(args.get("max_results") or 50),
        ),
        default_roles=None,
    ),
    ToolSpec(
        name="calendar_create_event",
        description=(
            "Create a Google Calendar event (and email invites when "
            "send_updates='all'). Set recurrence to an RRULE (e.g. "
            "'RRULE:FREQ=MONTHLY;BYDAY=SA;BYSETPOS=1') for a recurring series."
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": _ACCOUNT_PROP,
                "summary": {"type": "string", "description": "Event title."},
                "start": {
                    "type": "string",
                    "description": "ISO start datetime (e.g. 2026-10-15T09:00:00) or date for all-day.",
                },
                "end": {
                    "type": "string",
                    "description": "ISO end datetime or date.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee emails to invite.",
                },
                "description": {"type": "string", "description": "Event body/notes."},
                "location": {"type": "string", "description": "Event location."},
                "recurrence": {
                    "type": "string",
                    "description": "RRULE string for a recurring event (optional).",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone (default America/Los_Angeles).",
                },
                "send_updates": _SEND_UPDATES_PROP,
            },
            "required": ["summary", "start", "end"],
        },
        handler=lambda args, ctx: calendar_create_event(
            account=args.get("account"),
            summary=args.get("summary", ""),
            start=args.get("start", ""),
            end=args.get("end", ""),
            attendees=args.get("attendees"),
            description=args.get("description", ""),
            location=args.get("location", ""),
            recurrence=args.get("recurrence"),
            timezone=args.get("timezone") or DEFAULT_TIMEZONE,
            send_updates=args.get("send_updates") or "all",
        ),
        default_roles=None,
    ),
    ToolSpec(
        name="calendar_update_event",
        description=(
            "Update (PATCH) an existing Google Calendar event by id — change "
            "time, attendees, recurrence, etc. Omitted fields are left as-is."
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": _ACCOUNT_PROP,
                "event_id": {"type": "string", "description": "Event id to update."},
                "summary": {"type": "string"},
                "start": {"type": "string", "description": "ISO start."},
                "end": {"type": "string", "description": "ISO end."},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "recurrence": {"type": "string", "description": "RRULE string."},
                "timezone": {"type": "string"},
                "send_updates": _SEND_UPDATES_PROP,
            },
            "required": ["event_id"],
        },
        handler=lambda args, ctx: calendar_update_event(
            account=args.get("account"),
            event_id=args.get("event_id", ""),
            summary=args.get("summary"),
            start=args.get("start"),
            end=args.get("end"),
            attendees=args.get("attendees"),
            description=args.get("description"),
            location=args.get("location"),
            recurrence=args.get("recurrence"),
            timezone=args.get("timezone") or DEFAULT_TIMEZONE,
            send_updates=args.get("send_updates") or "all",
        ),
        default_roles=None,
    ),
    ToolSpec(
        name="calendar_delete_event",
        description="Delete a Google Calendar event by id.",
        parameters={
            "type": "object",
            "properties": {
                "account": _ACCOUNT_PROP,
                "event_id": {"type": "string", "description": "Event id to delete."},
                "send_updates": _SEND_UPDATES_PROP,
            },
            "required": ["event_id"],
        },
        handler=lambda args, ctx: calendar_delete_event(
            account=args.get("account"),
            event_id=args.get("event_id", ""),
            send_updates=args.get("send_updates") or "all",
        ),
        default_roles=None,
    ),
]
