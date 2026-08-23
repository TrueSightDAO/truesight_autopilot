"""Google Calendar tools for the autopilot agent — shared OAuth token with Gmail.

Four operations exposed to the model:

- ``calendar_create_event(summary, start, end, ...)`` — create an event.
- ``calendar_update_event(event_id, ...)`` — patch an existing event.
- ``calendar_get_event(event_id, account=None)`` — fetch a single event.
- ``calendar_list_events(query=None, time_min=None, time_max=None, ...)`` —
  search/list events.

Account resolution and token loading mirror ``gmail_tools.py`` exactly — the
same per-account token file (``{account}_token.json`` under
``GMAIL_TOKENS_DIR``) is used for both, since it now carries Gmail + Calendar
+ Drive scopes together (minted 2026-08-23).

Attachments gotcha (why this module exists instead of ad-hoc scripts):
Google Calendar's API silently drops the ``attachments`` field on write
unless the request explicitly passes ``supportsAttachments=True`` — this has
nothing to do with OAuth scope and is easy to miss when hand-rolling a
one-off script. ``calendar_create_event``/``calendar_update_event`` always
set it, so this class of mistake can't happen through this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.tools.calendar")

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_TOKENS_DIR = "/opt/truesight_autopilot/config/gmail"
_MAX_LIST_RESULTS = 50


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _resolve_account(account: str | None) -> str:
    return (account or os.environ.get("GMAIL_DEFAULT_ACCOUNT") or "admin").lower()


def _token_data(account: str) -> dict | None:
    """Load the OAuth token JSON for ``account`` — same file gmail_tools.py uses."""
    tokens_dir = Path(os.environ.get("GMAIL_TOKENS_DIR", DEFAULT_TOKENS_DIR))
    candidate = tokens_dir / f"{account}_token.json"
    if candidate.is_file():
        try:
            return json.loads(candidate.read_text())
        except Exception as e:
            logger.warning("Failed to parse %s: %s", candidate, e)
    return None


def _build_service(account: str | None):
    """Returns (service, error_json_or_None)."""
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


def _event_summary(event: dict) -> dict:
    return {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "start": event.get("start"),
        "end": event.get("end"),
        "location": event.get("location"),
        "description": event.get("description"),
        "attachments": event.get("attachments", []),
        "html_link": event.get("htmlLink"),
    }


# ── create / update ──────────────────────────────────────────────────────


def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    timezone: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attachments: list[dict] | None = None,
    reminders_minutes: list[int] | None = None,
    account: str | None = None,
) -> str:
    if not summary or not start or not end:
        return _err("summary, start, and end are required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": timezone} if timezone else {"dateTime": start},
        "end": {"dateTime": end, "timeZone": timezone} if timezone else {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attachments:
        body["attachments"] = attachments
    if reminders_minutes:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminders_minutes],
        }

    try:
        event = (
            service.events()
            .insert(calendarId="primary", body=body, supportsAttachments=True)
            .execute()
        )
    except Exception as e:
        return _err(str(e), summary=summary)

    logger.info(
        "calendar_create_event ok: account=%s id=%s attachments=%d",
        _resolve_account(account),
        event.get("id"),
        len(attachments or []),
    )
    return json.dumps({"status": "ok", "account": _resolve_account(account), "event": _event_summary(event)})


def calendar_update_event(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attachments: list[dict] | None = None,
    account: str | None = None,
) -> str:
    if not event_id:
        return _err("event_id is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]

    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = {"dateTime": start, "timeZone": timezone} if timezone else {"dateTime": start}
    if end is not None:
        body["end"] = {"dateTime": end, "timeZone": timezone} if timezone else {"dateTime": end}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if attachments is not None:
        body["attachments"] = attachments
    if not body:
        return _err("at least one field to update is required")

    try:
        event = (
            service.events()
            .patch(calendarId="primary", eventId=event_id, body=body, supportsAttachments=True)
            .execute()
        )
    except Exception as e:
        return _err(str(e), event_id=event_id)

    logger.info("calendar_update_event ok: account=%s id=%s", _resolve_account(account), event_id)
    return json.dumps({"status": "ok", "account": _resolve_account(account), "event": _event_summary(event)})


# ── read ──────────────────────────────────────────────────────────────────


def calendar_get_event(event_id: str, account: str | None = None) -> str:
    if not event_id:
        return _err("event_id is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        event = service.events().get(calendarId="primary", eventId=event_id).execute()
    except Exception as e:
        return _err(str(e), event_id=event_id)
    return json.dumps({"status": "ok", "account": _resolve_account(account), "event": _event_summary(event)})


def calendar_list_events(
    query: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 10,
    account: str | None = None,
) -> str:
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    max_results = max(1, min(int(max_results or 10), _MAX_LIST_RESULTS))
    params: dict[str, Any] = {
        "calendarId": "primary",
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if query:
        params["q"] = query
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    try:
        resp = service.events().list(**params).execute()
    except Exception as e:
        return _err(str(e), query=query)
    events = [_event_summary(e) for e in resp.get("items", [])]
    logger.info(
        "calendar_list_events ok: account=%s q=%.60s hits=%d",
        _resolve_account(account),
        query or "",
        len(events),
    )
    return json.dumps(
        {
            "status": "ok",
            "account": _resolve_account(account),
            "result_count": len(events),
            "events": events,
        }
    )


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

_ACCOUNT_ENUM = ["admin", "gary"]
_ATTACHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "fileUrl": {"type": "string", "description": "Google Drive file URL (the file must already be uploaded and readable)."},
        "title": {"type": "string", "description": "Display title for the attachment."},
        "mimeType": {"type": "string", "description": "MIME type, e.g. application/pdf."},
    },
    "required": ["fileUrl"],
}

TOOL_SPECS = [
    ToolSpec(
        name="calendar_create_event",
        description="Create a Google Calendar event. Supports Drive-file attachments (e.g. a ticket PDF) — always pass fully-qualified Drive fileUrls, not local paths; upload to Drive first if needed.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start": {"type": "string", "description": "ISO 8601 start datetime, e.g. 2026-08-26T09:00:00."},
                "end": {"type": "string", "description": "ISO 8601 end datetime."},
                "timezone": {"type": "string", "description": "IANA timezone, e.g. America/Sao_Paulo. Defaults to the calendar's own timezone if omitted."},
                "description": {"type": "string", "description": "Event description/notes."},
                "location": {"type": "string", "description": "Event location."},
                "attachments": {"type": "array", "items": _ATTACHMENT_SCHEMA, "description": "Drive-file attachments."},
                "reminders_minutes": {"type": "array", "items": {"type": "integer"}, "description": "Popup reminder offsets in minutes before the event, e.g. [120, 30]."},
                "account": {"type": "string", "description": "Mailbox/calendar label.", "enum": _ACCOUNT_ENUM},
            },
            "required": ["summary", "start", "end"],
        },
        handler=lambda args, ctx: calendar_create_event(
            summary=args.get("summary", ""),
            start=args.get("start", ""),
            end=args.get("end", ""),
            timezone=args.get("timezone"),
            description=args.get("description"),
            location=args.get("location"),
            attachments=args.get("attachments"),
            reminders_minutes=args.get("reminders_minutes"),
            account=args.get("account"),
        ),
    ),
    ToolSpec(
        name="calendar_update_event",
        description="Patch an existing Google Calendar event — only the fields provided are changed. Use this to attach a Drive file to an event created earlier (pass attachments); this always sets supportsAttachments so the attachment actually persists (the Calendar API silently drops it otherwise, independent of OAuth scope).",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Calendar event ID (from calendar_create_event or calendar_list_events)."},
                "summary": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start datetime."},
                "end": {"type": "string", "description": "ISO 8601 end datetime."},
                "timezone": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attachments": {"type": "array", "items": _ATTACHMENT_SCHEMA, "description": "Replaces the event's attachment list."},
                "account": {"type": "string", "description": "Mailbox/calendar label.", "enum": _ACCOUNT_ENUM},
            },
            "required": ["event_id"],
        },
        handler=lambda args, ctx: calendar_update_event(
            event_id=args.get("event_id", ""),
            summary=args.get("summary"),
            start=args.get("start"),
            end=args.get("end"),
            timezone=args.get("timezone"),
            description=args.get("description"),
            location=args.get("location"),
            attachments=args.get("attachments"),
            account=args.get("account"),
        ),
    ),
    ToolSpec(
        name="calendar_get_event",
        description="Fetch a single Google Calendar event by ID.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Calendar event ID."},
                "account": {"type": "string", "description": "Mailbox/calendar label.", "enum": _ACCOUNT_ENUM},
            },
            "required": ["event_id"],
        },
        handler=lambda args, ctx: calendar_get_event(
            event_id=args.get("event_id", ""),
            account=args.get("account"),
        ),
    ),
    ToolSpec(
        name="calendar_list_events",
        description="Search/list Google Calendar events. Use to find an event's ID before updating it, or to check for scheduling conflicts.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search (matches summary/description/location)."},
                "time_min": {"type": "string", "description": "ISO 8601 lower bound (inclusive)."},
                "time_max": {"type": "string", "description": "ISO 8601 upper bound (exclusive)."},
                "max_results": {"type": "integer", "description": "Max events (1-50).", "default": 10},
                "account": {"type": "string", "description": "Mailbox/calendar label.", "enum": _ACCOUNT_ENUM},
            },
        },
        handler=lambda args, ctx: calendar_list_events(
            query=args.get("query"),
            time_min=args.get("time_min"),
            time_max=args.get("time_max"),
            max_results=args.get("max_results", 10),
            account=args.get("account"),
        ),
    ),
]
