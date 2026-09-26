"""Unit tests for image_generation_tools.py (Gemini API mocked)."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools import image_generation_tools as igt


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch, tmp_path):
    monkeypatch.setattr(igt, "_ATTACH_DIR", str(tmp_path))


def test_missing_prompt_returns_error():
    out = json.loads(igt.generate_image(""))
    assert out["status"] == "error"
    assert "prompt is required" in out["reason"]


def test_missing_api_key_returns_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = json.loads(igt.generate_image("a cacao pod"))
    assert out["status"] == "error"
    assert "GEMINI_API_KEY" in out["reason"]


def _fake_response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = json.dumps(payload or {})
    return resp


def test_generate_image_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    fake_png_bytes = b"\x89PNG\r\n\x1a\nfakeimagedata"
    encoded = base64.b64encode(fake_png_bytes).decode("ascii")
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Here you go"},
                        {"inlineData": {"mimeType": "image/png", "data": encoded}},
                    ]
                }
            }
        ]
    }
    with patch.object(igt.requests, "post", return_value=_fake_response(200, payload)):
        out = json.loads(igt.generate_image("a cacao pod", filename="test_image"))

    assert out["status"] == "ok"
    assert out["mime_type"] == "image/png"
    assert out["path"].endswith("test_image.png")
    assert (tmp_path / "test_image.png").read_bytes() == fake_png_bytes


def test_generate_image_http_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with patch.object(igt.requests, "post", return_value=_fake_response(403, {"error": "denied"})):
        out = json.loads(igt.generate_image("a cacao pod"))
    assert out["status"] == "error"
    assert "403" in out["reason"]


def test_generate_image_no_image_in_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    payload = {"candidates": [{"content": {"parts": [{"text": "no image sorry"}]}}]}
    with patch.object(igt.requests, "post", return_value=_fake_response(200, payload)):
        out = json.loads(igt.generate_image("a cacao pod"))
    assert out["status"] == "error"
    assert "no image data" in out["reason"]


def test_generate_image_request_exception(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    import requests as requests_mod

    with patch.object(igt.requests, "post", side_effect=requests_mod.RequestException("boom")):
        out = json.loads(igt.generate_image("a cacao pod"))
    assert out["status"] == "error"
    assert "Gemini request failed" in out["reason"]


def test_generate_image_default_filename(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    encoded = base64.b64encode(b"data").decode("ascii")
    payload = {
        "candidates": [
            {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}}
        ]
    }
    with patch.object(igt.requests, "post", return_value=_fake_response(200, payload)):
        out = json.loads(igt.generate_image("a cacao pod"))
    assert out["status"] == "ok"
    assert out["path"].startswith(str(tmp_path))
    assert out["path"].endswith(".png")


def test_tool_specs_registered():
    names = {spec.name for spec in igt.TOOL_SPECS}
    assert names == {"generate_image"}
