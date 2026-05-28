"""Unit tests for the Gmail tools (Gmail API mocked)."""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools import gmail_tools as gt


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKENS_DIR", str(tmp_path))
    monkeypatch.delenv("GMAIL_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)


def _write_token(tmp_path, account: str):
    (tmp_path / f"{account}_token.json").write_text(json.dumps({
        "token": "ya29.fake",
        "refresh_token": "1//fake",
        "client_id": "id",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "token_uri": "https://oauth2.googleapis.com/token",
    }))


def test_credentials_missing_returns_error():
    out = json.loads(gt.gmail_search("from:x@y.com"))
    assert out["status"] == "error"
    assert "credentials missing" in out["reason"]


def test_missing_query_returns_error(tmp_path):
    _write_token(tmp_path, "admin")
    out = json.loads(gt.gmail_search(""))
    assert out["status"] == "error"


def _mock_service():
    """Build a MagicMock that mimics the Gmail service .users() chain."""
    service = MagicMock()
    return service


def test_search_happy_path(tmp_path):
    _write_token(tmp_path, "admin")
    service = _mock_service()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}],
    }
    def fake_get(userId, id, format, metadataHeaders=None):
        exec_mock = MagicMock()
        exec_mock.execute.return_value = {
            "id": id,
            "threadId": f"t-{id}",
            "snippet": f"snippet-{id}",
            "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "Subject", "value": f"Subj {id}"},
                {"name": "Date", "value": "Thu, 28 May 2026 00:00:00 +0000"},
            ]},
        }
        return exec_mock
    service.users.return_value.messages.return_value.get.side_effect = fake_get

    with patch.object(gt, "_build_service", return_value=(service, None)):
        out = json.loads(gt.gmail_search("from:a@b.com", account="admin"))

    assert out["status"] == "ok"
    assert out["account"] == "admin"
    assert out["result_count"] == 2
    assert out["results"][0]["id"] == "m1"
    assert out["results"][0]["subject"] == "Subj m1"


def test_read_message_extracts_plain_text(tmp_path):
    _write_token(tmp_path, "gary")
    service = _mock_service()
    encoded = base64.urlsafe_b64encode(b"Hello from the payload").decode("ascii")
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "Hello",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "Subject", "value": "Hi"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded}},
                {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<b>html</b>").decode("ascii")}},
            ],
        },
    }

    with patch.object(gt, "_build_service", return_value=(service, None)):
        out = json.loads(gt.gmail_read_message("m1", account="gary"))

    assert out["status"] == "ok"
    assert out["account"] == "gary"
    assert "Hello from the payload" in out["body"]
    assert out["truncated"] is False


def test_send_builds_raw_payload(tmp_path):
    _write_token(tmp_path, "admin")
    service = _mock_service()
    captured = {}
    def capture_send(userId, body):
        captured["body"] = body
        exec_mock = MagicMock()
        exec_mock.execute.return_value = {"id": "sent-1", "threadId": "t1", "labelIds": ["SENT"]}
        return exec_mock
    service.users.return_value.messages.return_value.send.side_effect = capture_send

    with patch.object(gt, "_build_service", return_value=(service, None)):
        out = json.loads(gt.gmail_send(
            to="p@q.com", subject="Re: hi", body="Hello\nthere.",
            account="admin", cc="r@q.com",
        ))

    assert out["status"] == "ok"
    assert out["id"] == "sent-1"
    raw = base64.urlsafe_b64decode(captured["body"]["raw"].encode("ascii")).decode("utf-8")
    assert "To: p@q.com" in raw
    assert "Cc: r@q.com" in raw
    assert "Subject: Re: hi" in raw
    # MIMEText with utf-8 charset uses base64 transfer encoding by default.
    assert base64.b64encode(b"Hello\nthere.").decode("ascii") in raw


def test_create_draft_uses_drafts_create(tmp_path):
    _write_token(tmp_path, "admin")
    service = _mock_service()
    service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
        "id": "d1",
        "message": {"id": "m1"},
    }

    with patch.object(gt, "_build_service", return_value=(service, None)):
        out = json.loads(gt.gmail_create_draft(
            to="p@q.com", subject="draft test", body="body", account="admin",
        ))

    assert out["status"] == "ok"
    assert out["draft_id"] == "d1"
    assert out["message_id"] == "m1"


def test_apply_label_requires_at_least_one(tmp_path):
    _write_token(tmp_path, "admin")
    with patch.object(gt, "_build_service", return_value=(_mock_service(), None)):
        out = json.loads(gt.gmail_apply_label("m1"))
    assert out["status"] == "error"


def test_resolve_account_uses_default_env(monkeypatch):
    monkeypatch.setenv("GMAIL_DEFAULT_ACCOUNT", "gary")
    assert gt._resolve_account(None) == "gary"
    assert gt._resolve_account("ADMIN") == "admin"


def test_legacy_env_fallback_for_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKENS_DIR", str(tmp_path))
    # No admin_token.json on disk — should fall back to env var.
    monkeypatch.setenv("GMAIL_TOKEN_JSON", json.dumps({
        "token": "ya29.fake", "refresh_token": "1//fake",
        "client_id": "id", "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    data = gt._token_data("admin")
    assert data is not None
    assert data["client_id"] == "id"

    # gary has no env fallback.
    assert gt._token_data("gary") is None
