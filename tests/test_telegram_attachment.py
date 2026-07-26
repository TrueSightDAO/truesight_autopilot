"""Tests for send_telegram_attachment (app/tools/telegram_attachment.py).

Added 2026-07-26 alongside the tool itself — lets Sophia attach a local file
(already generated/downloaded) to the current Telegram conversation instead
of only sharing a link, for governors behind a firewall that blocks
github.com / the AWS console / etc. but not Telegram.
"""

from __future__ import annotations

import json
import os
import tempfile

import httpx

from app.tools import telegram_attachment as ta


def _tmp_file(content: bytes = b"hello world") -> str:
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


# ── _thread_id_from_session (pure) ──────────────────────────────────────────


def test_thread_id_from_tg_session():
    assert ta._thread_id_from_session("abc123:tg:-1001234567890:1955") == 1955


def test_thread_id_from_session_zero_is_none():
    # build_session_id uses ":0" for General/no-topic — treat as "no thread".
    assert ta._thread_id_from_session("abc123:tg:-1001234567890:0") is None


def test_thread_id_from_non_tg_session_is_none():
    assert ta._thread_id_from_session("abc123:web-session-xyz") is None
    assert ta._thread_id_from_session(None) is None


# ── Validation ───────────────────────────────────────────────────────────────


def test_requires_file_path():
    out = ta.send_telegram_attachment(file_path="  ")
    assert out["status"] == "error" and "file_path" in out["reason"]


def test_missing_file_errors():
    out = ta.send_telegram_attachment(file_path="/tmp/does-not-exist-12345.pdf")
    assert out["status"] == "error" and "not found" in out["reason"]


def test_empty_file_errors():
    path = _tmp_file(b"")
    try:
        out = ta.send_telegram_attachment(file_path=path, chat_id="-100123")
        assert out["status"] == "error" and "empty" in out["reason"]
    finally:
        os.remove(path)


def test_oversized_file_errors(monkeypatch):
    monkeypatch.setattr(ta, "_MAX_BYTES", 5)  # tiny cap so our test file trips it
    path = _tmp_file(b"this is definitely more than 5 bytes")
    try:
        out = ta.send_telegram_attachment(file_path=path, chat_id="-100123")
        assert out["status"] == "error" and "exceeds Telegram's" in out["reason"]
    finally:
        os.remove(path)


def test_missing_token_errors(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "", raising=False)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(file_path=path, chat_id="-100123")
        assert out["status"] == "error" and "TELEGRAM_BOT_API_KEY" in out["reason"]
    finally:
        os.remove(path)


def test_no_target_chat_errors(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(ta.settings, "telegram_home_group_id", "", raising=False)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(file_path=path, session_id="pub:web-xyz")
        assert out["status"] == "error" and "chat_id" in out["reason"]
    finally:
        os.remove(path)


def test_non_numeric_thread_id_errors(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(
            file_path=path, chat_id="-100123", thread_id="not-a-number"
        )
        assert out["status"] == "error" and "thread_id" in out["reason"]
    finally:
        os.remove(path)


# ── Success + fallback paths (mocked httpx.post) ────────────────────────────


def test_send_uses_thread_from_session(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    seen = {}

    def fake_post(url, data=None, files=None, timeout=None):
        seen["url"] = url
        seen["data"] = data
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(
            file_path=path, session_id="abc:tg:-1001234567890:1955"
        )
        assert out["status"] == "ok"
        assert out["chat_id"] == "-1001234567890"
        assert out["message_thread_id"] == 1955
        assert seen["data"]["message_thread_id"] == 1955
        assert "sendDocument" in seen["url"]
    finally:
        os.remove(path)


def test_explicit_chat_and_thread_override_session(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)

    def fake_post(url, data=None, files=None, timeout=None):
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 7}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(
            file_path=path,
            chat_id="-1009999",
            thread_id=1,
            session_id="abc:tg:-1001234567890:1955",
        )
        assert out["status"] == "ok"
        assert out["chat_id"] == "-1009999"
        assert out["message_thread_id"] == 1
    finally:
        os.remove(path)


def test_caption_is_passed_and_truncated(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    seen = {}

    def fake_post(url, data=None, files=None, timeout=None):
        seen["caption"] = data.get("caption")
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 1}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        long_caption = "x" * 2000
        ta.send_telegram_attachment(
            file_path=path, chat_id="-100123", caption=long_caption
        )
        assert len(seen["caption"]) == ta._CAPTION_LIMIT
    finally:
        os.remove(path)


def test_thread_not_found_falls_back_without_thread(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(dict(data or {}))
        if "message_thread_id" in (data or {}):
            return httpx.Response(
                400,
                json={"ok": False, "description": "Bad Request: message thread not found"},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 99}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(
            file_path=path, chat_id="-100123", thread_id=404
        )
        assert out["status"] == "ok"
        assert "note" in out and "404" in out["note"]
        assert len(calls) == 2  # first attempt with thread, retry without
        assert "message_thread_id" not in calls[1]
    finally:
        os.remove(path)


def test_other_telegram_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(1)
        return httpx.Response(
            403,
            json={"ok": False, "description": "Forbidden: bot was blocked by the user"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        out = ta.send_telegram_attachment(file_path=path, chat_id="-100123")
        assert out["status"] == "error"
        assert "Forbidden" in out["reason"]
        assert len(calls) == 1  # no retry for a non-thread error
    finally:
        os.remove(path)


# ── TOOL_SPEC wiring ─────────────────────────────────────────────────────────


def test_tool_spec_handler_roundtrips(monkeypatch):
    monkeypatch.setattr(ta.settings, "telegram_bot_api_key", "dummy", raising=False)

    def fake_post(url, data=None, files=None, timeout=None):
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 5}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    path = _tmp_file()
    try:
        result = json.loads(
            ta.TOOL_SPEC.handler(
                {"file_path": path},
                {"session_id": "abc:tg:-1001234567890:1955"},
            )
        )
        assert result["status"] == "ok"
    finally:
        os.remove(path)


def test_tool_spec_required_fields():
    assert ta.TOOL_SPEC.parameters["required"] == ["file_path"]
    assert ta.TOOL_SPEC.default_roles is None
