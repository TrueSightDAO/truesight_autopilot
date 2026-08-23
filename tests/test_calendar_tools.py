"""Unit tests for the Calendar tools (Calendar API mocked)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools import calendar_tools as ct


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKENS_DIR", str(tmp_path))
    monkeypatch.delenv("GMAIL_DEFAULT_ACCOUNT", raising=False)


def _write_token(tmp_path, account: str):
    (tmp_path / f"{account}_token.json").write_text(
        json.dumps(
            {
                "token": "ya29.fake",
                "refresh_token": "1//fake",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": ["https://www.googleapis.com/auth/calendar"],
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )


def test_credentials_missing_returns_error():
    out = json.loads(ct.calendar_create_event("Test", "2026-01-01T09:00:00", "2026-01-01T10:00:00"))
    assert out["status"] == "error"
    assert "credentials missing" in out["reason"]


def test_create_event_requires_fields(tmp_path):
    _write_token(tmp_path, "admin")
    out = json.loads(ct.calendar_create_event("", "", ""))
    assert out["status"] == "error"


def test_create_event_always_passes_supports_attachments(tmp_path):
    """Regression test for the actual bug: Calendar API silently drops
    attachments unless supportsAttachments=True is set on the write call."""
    _write_token(tmp_path, "admin")
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Test",
        "attachments": [{"fileUrl": "https://drive.google.com/x", "title": "x.pdf"}],
    }

    with patch.object(ct, "_build_service", return_value=(service, None)):
        out = json.loads(
            ct.calendar_create_event(
                "Test",
                "2026-01-01T09:00:00",
                "2026-01-01T10:00:00",
                attachments=[{"fileUrl": "https://drive.google.com/x", "title": "x.pdf"}],
            )
        )

    assert out["status"] == "ok"
    assert out["event"]["attachments"]
    call_kwargs = service.events.return_value.insert.call_args.kwargs
    assert call_kwargs["supportsAttachments"] is True
    assert call_kwargs["body"]["attachments"] == [{"fileUrl": "https://drive.google.com/x", "title": "x.pdf"}]


def test_update_event_always_passes_supports_attachments(tmp_path):
    _write_token(tmp_path, "admin")
    service = MagicMock()
    service.events.return_value.patch.return_value.execute.return_value = {
        "id": "evt1",
        "attachments": [{"fileUrl": "https://drive.google.com/x"}],
    }

    with patch.object(ct, "_build_service", return_value=(service, None)):
        out = json.loads(
            ct.calendar_update_event("evt1", attachments=[{"fileUrl": "https://drive.google.com/x"}])
        )

    assert out["status"] == "ok"
    call_kwargs = service.events.return_value.patch.call_args.kwargs
    assert call_kwargs["supportsAttachments"] is True
    assert call_kwargs["eventId"] == "evt1"


def test_update_event_requires_event_id(tmp_path):
    _write_token(tmp_path, "admin")
    out = json.loads(ct.calendar_update_event(""))
    assert out["status"] == "error"


def test_update_event_requires_at_least_one_field(tmp_path):
    _write_token(tmp_path, "admin")
    service = MagicMock()
    with patch.object(ct, "_build_service", return_value=(service, None)):
        out = json.loads(ct.calendar_update_event("evt1"))
    assert out["status"] == "error"
    assert "at least one field" in out["reason"]


def test_get_event_happy_path(tmp_path):
    _write_token(tmp_path, "admin")
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Test",
    }
    with patch.object(ct, "_build_service", return_value=(service, None)):
        out = json.loads(ct.calendar_get_event("evt1"))
    assert out["status"] == "ok"
    assert out["event"]["id"] == "evt1"


def test_get_event_requires_event_id(tmp_path):
    _write_token(tmp_path, "admin")
    out = json.loads(ct.calendar_get_event(""))
    assert out["status"] == "error"


def test_list_events_happy_path(tmp_path):
    _write_token(tmp_path, "admin")
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "evt1", "summary": "A"}, {"id": "evt2", "summary": "B"}],
    }
    with patch.object(ct, "_build_service", return_value=(service, None)):
        out = json.loads(ct.calendar_list_events(query="Startup"))
    assert out["status"] == "ok"
    assert out["result_count"] == 2
    call_kwargs = service.events.return_value.list.call_args.kwargs
    assert call_kwargs["q"] == "Startup"
    assert call_kwargs["singleEvents"] is True


def test_list_events_caps_max_results(tmp_path):
    _write_token(tmp_path, "admin")
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    with patch.object(ct, "_build_service", return_value=(service, None)):
        ct.calendar_list_events(max_results=999)
    call_kwargs = service.events.return_value.list.call_args.kwargs
    assert call_kwargs["maxResults"] == ct._MAX_LIST_RESULTS


def test_tool_specs_registered():
    names = {spec.name for spec in ct.TOOL_SPECS}
    assert names == {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_get_event",
        "calendar_list_events",
    }
