"""Unit tests for the Google Calendar tool + watchdog pure helpers.

Hermetic — no network, no credentials. Exercises the pure/coercion helpers and
the digest logic that the calendar watchdog relies on.
"""

import json

from app.calendar_watchdog import _needs_attention, _parse_dt, build_digest
from app.tools import google_calendar as gc
from app.tools.google_calendar import (
    _event_summary,
    _normalize_attendees,
    _normalize_recurrence,
    _time_block,
)


# ── google_calendar pure helpers ─────────────────────────────────────────────


def test_normalize_recurrence_bare_rrule():
    assert _normalize_recurrence("RRULE:FREQ=DAILY") == ["RRULE:FREQ=DAILY"]


def test_normalize_recurrence_bare_body_gets_prefix():
    assert _normalize_recurrence("FREQ=MONTHLY;BYDAY=SA;BYSETPOS=1") == [
        "RRULE:FREQ=MONTHLY;BYDAY=SA;BYSETPOS=1"
    ]


def test_normalize_recurrence_list_and_blank():
    assert _normalize_recurrence(["FREQ=DAILY", "RRULE:FREQ=WEEKLY"]) == [
        "RRULE:FREQ=DAILY",
        "RRULE:FREQ=WEEKLY",
    ]
    assert _normalize_recurrence("") is None
    assert _normalize_recurrence(None) is None
    assert _normalize_recurrence(["", "  "]) is None


def test_normalize_attendees_strings_and_dicts():
    assert _normalize_attendees(["a@x.com", " b@y.com "]) == [
        {"email": "a@x.com"},
        {"email": "b@y.com"},
    ]
    assert _normalize_attendees([{"email": "c@z.com", "displayName": "Cee"}]) == [
        {"email": "c@z.com", "displayName": "Cee"}
    ]
    assert _normalize_attendees([]) is None
    assert _normalize_attendees([{"displayName": "no-email"}]) is None


def test_time_block_datetime_and_date():
    assert _time_block("2026-10-15T09:00:00", "America/Los_Angeles") == {
        "dateTime": "2026-10-15T09:00:00",
        "timeZone": "America/Los_Angeles",
    }
    assert _time_block("2026-10-15", None) == {"date": "2026-10-15"}
    assert _time_block("", None) is None
    assert _time_block(None, None) is None


def test_event_summary_condenses_fields():
    ev = {
        "id": "abc",
        "summary": "Walk",
        "start": {"dateTime": "2026-11-07T09:00:00-08:00"},
        "end": {"dateTime": "2026-11-07T09:30:00-08:00"},
        "htmlLink": "http://x",
        "recurrence": ["RRULE:FREQ=MONTHLY"],
        "attendees": [{"email": "G@X.com", "responseStatus": "accepted"}],
    }
    out = _event_summary(ev)
    assert out["id"] == "abc"
    assert out["start"].startswith("2026-11-07T09:00")
    assert out["attendees"][0]["email"] == "g@x.com"
    assert out["attendees"][0]["status"] == "accepted"
    # missing title falls back
    assert _event_summary({"id": "z"})["summary"] == "(no title)"


def test_create_event_requires_summary_and_times():
    # Missing summary → error; no creds touched (validation short-circuits first).
    r = json.loads(
        gc.calendar_create_event(summary="", start="2026-01-01", end="2026-01-02")
    )
    assert r["status"] == "error" and "summary" in r["reason"]
    r = json.loads(gc.calendar_create_event(summary="x", start="", end=""))
    assert r["status"] == "error" and "start and end" in r["reason"]


def test_update_and_delete_require_event_id():
    assert json.loads(gc.calendar_update_event(event_id=""))["status"] == "error"
    assert json.loads(gc.calendar_delete_event(event_id=""))["status"] == "error"


def test_missing_credentials_degrade_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKENS_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_TOKEN_JSON", "")
    r = json.loads(gc.calendar_list_events(account="admin"))
    assert r["status"] == "error"
    assert "credentials missing" in r["reason"]


# ── watchdog pure helpers ────────────────────────────────────────────────────


def test_parse_dt_variants():
    assert _parse_dt("2026-10-15T09:00:00Z").tzinfo is not None
    assert _parse_dt("2026-10-15").hour == 0
    assert _parse_dt("") is None
    assert _parse_dt("not-a-date") is None


def test_needs_attention_soon_event():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    ev = {"summary": "X", "start": "2026-10-15T08:00:00+00:00", "attendees": []}
    reasons = _needs_attention(ev, now)
    assert any("starts in" in r for r in reasons)


def test_needs_attention_flags_pending_attendees():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    ev = {
        "summary": "X",
        "start": "2026-10-18T08:00:00+00:00",
        "attendees": [
            {"email": "a@x.com", "status": "needsAction"},
            {"email": "b@x.com", "status": "accepted"},
        ],
    }
    reasons = _needs_attention(ev, now)
    assert any("awaiting response: a@x.com" in r for r in reasons)


def test_needs_attention_quiet_when_all_answered():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    ev = {
        "summary": "X",
        "start": "2026-10-30T08:00:00+00:00",
        "attendees": [{"email": "a@x.com", "status": "accepted"}],
    }
    assert _needs_attention(ev, now) == []


def test_build_digest_empty_when_nothing_needs_attention():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    ev = {"summary": "Far", "start": "2027-01-01T08:00:00+00:00", "attendees": []}
    assert build_digest([ev], now) == ""


def test_build_digest_renders_header_and_rows():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    ev = {
        "summary": "Walk",
        "start": "2026-10-15T08:00:00+00:00",
        "attendees": [{"email": "a@x.com", "status": "needsAction"}],
    }
    out = build_digest([ev], now)
    assert "Calendar self-check" in out
    assert "Walk" in out
    assert "a@x.com" in out
