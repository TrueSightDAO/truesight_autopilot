"""Unit tests for the http_fetch tool (httpx mocked)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.tools import http_fetch as hf


def _fake_response(body: bytes, status: int = 200, content_type: str = "text/plain"):
    resp = MagicMock(spec=httpx.Response)
    resp.content = body
    resp.status_code = status
    resp.url = "https://example.com/x"
    resp.headers = {"content-type": content_type, "content-length": str(len(body))}
    return resp


def _mock_client(monkeypatch, fake_response):
    fake_client_ctx = MagicMock()
    fake_client_ctx.__enter__.return_value.request.return_value = fake_response
    fake_client_ctx.__exit__.return_value = False
    monkeypatch.setattr(hf.httpx, "Client", lambda **kwargs: fake_client_ctx)


def test_missing_url_returns_error():
    out = json.loads(hf.http_fetch(""))
    assert out["status"] == "error"


def test_disallowed_method_returns_error():
    out = json.loads(hf.http_fetch("https://example.com", method="CONNECT"))
    assert out["status"] == "error"
    assert "CONNECT" in out["reason"]


def test_blocks_loopback():
    out = json.loads(hf.http_fetch("http://127.0.0.1:8001/health"))
    assert out["status"] == "error"
    assert "private" in out["reason"].lower()


def test_blocks_metadata_host():
    out = json.loads(hf.http_fetch("http://169.254.169.254/latest/meta-data/"))
    assert out["status"] == "error"


def test_happy_path_text_response(monkeypatch):
    _mock_client(monkeypatch, _fake_response(b"hello world"))
    out = json.loads(hf.http_fetch("https://example.com/x"))
    assert out["status"] == "ok"
    assert out["status_code"] == 200
    assert out["body"] == "hello world"
    assert out["truncated"] is False
    assert out["encoding"] == "text"


def test_body_capped_at_256kb(monkeypatch):
    huge = b"a" * (260 * 1024)  # 260KB > 256KB cap
    _mock_client(monkeypatch, _fake_response(huge))
    out = json.loads(hf.http_fetch("https://example.com/big"))
    assert out["status"] == "ok"
    assert out["truncated"] is True
    assert out["byte_count"] == 256 * 1024


def test_json_body_auto_serialised(monkeypatch):
    fake_response = _fake_response(b'{"ok": true}', content_type="application/json")
    captured = {}

    fake_client_ctx = MagicMock()
    def capture_request(method, url, headers=None, content=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        return fake_response
    fake_client_ctx.__enter__.return_value.request.side_effect = capture_request
    fake_client_ctx.__exit__.return_value = False
    monkeypatch.setattr(hf.httpx, "Client", lambda **kwargs: fake_client_ctx)

    json.loads(hf.http_fetch(
        "https://example.com/api",
        method="POST",
        body={"event": "ping"},
    ))
    assert captured["method"] == "POST"
    assert captured["content"] == '{"event": "ping"}'
    assert captured["headers"].get("Content-Type") == "application/json"
