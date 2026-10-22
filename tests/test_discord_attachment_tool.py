"""Tests for the Discord attachment/post TOOLS (app/tools/discord_attachment.py).

Added alongside the tool itself — gives the agent an agent-callable way to post
a message or a file INTO a Discord channel (parity with the Telegram
send_telegram_attachment / post_to_telegram_topic surface).

No network: discord_adapter._api and _upload_file are monkeypatched.
"""

from __future__ import annotations

import json
import os
import tempfile

from app import discord_adapter as da
from app import policy
from app.tools import discord_attachment as datt


def _tmp_file(content: bytes = b"%PDF-1.4 hello") -> str:
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


# ── pure helpers ─────────────────────────────────────────────────────────


def test_channel_id_from_session():
    assert datt._channel_id_from_session("dc:111:222") == "222"
    assert datt._channel_id_from_session("seed:dc:111:222") == "222"
    assert datt._channel_id_from_session("tg:1:2") is None
    assert datt._channel_id_from_session(None) is None


def test_resolve_channel_id_precedence():
    assert datt._resolve_channel_id("EXPLICIT", "dc:1:FROM") == "EXPLICIT"
    assert datt._resolve_channel_id(None, "dc:1:FROM") == "FROM"
    assert datt._resolve_channel_id(None, None) is None


# ── post_discord_message ─────────────────────────────────────────────────


def test_post_requires_message():
    out = datt.post_discord_message("  ", channel_id="C")
    assert out["status"] == "error" and "message is required" in out["reason"]


def test_post_requires_channel():
    out = datt.post_discord_message("hi", session_id="tg:1:2")
    assert out["status"] == "error" and "no target channel" in out["reason"]


def test_post_dry_run_does_not_call_api(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", True, raising=False)

    def _boom(*a, **k):
        raise AssertionError("_api must not be called during dry-run")

    monkeypatch.setattr(da, "_api", _boom)
    out = datt.post_discord_message("hello", channel_id="C")
    assert out["status"] == "ok"
    assert out["message_ids"] == ["dry-run"]


def test_post_calls_api_when_live(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False, raising=False)
    calls = []

    def _fake(method, path, payload=None):
        calls.append((method, path, payload))
        return {"id": "msg123"}

    monkeypatch.setattr(da, "_api", _fake)
    out = datt.post_discord_message("hello", channel_id="C")
    assert out["status"] == "ok" and out["message_ids"] == ["msg123"]
    assert calls[0] == ("POST", "/channels/C/messages", {"content": "hello"})


def test_post_reports_api_failure(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False, raising=False)
    monkeypatch.setattr(da, "_api", lambda *a, **k: None)
    out = datt.post_discord_message("hello", channel_id="C")
    assert out["status"] == "error" and out["channel_id"] == "C"


# ── send_discord_attachment ──────────────────────────────────────────────


def test_attach_requires_file_path():
    out = datt.send_discord_attachment(file_path="  ")
    assert out["status"] == "error" and "file_path" in out["reason"]


def test_attach_missing_file_errors():
    out = datt.send_discord_attachment(file_path="/tmp/nope-12345.pdf")
    assert out["status"] == "error" and "not found" in out["reason"]


def test_attach_empty_file_errors():
    path = _tmp_file(b"")
    try:
        out = datt.send_discord_attachment(file_path=path, channel_id="C")
        assert out["status"] == "error" and "empty" in out["reason"]
    finally:
        os.remove(path)


def test_attach_requires_channel(monkeypatch):
    path = _tmp_file()
    try:
        # A session with NO platform marker still hard-errors (unchanged contract).
        # (A *tg:-marked* session now reroutes to Telegram instead -- see
        # tests/test_delivery_target.py::test_discord_tool_reroutes_on_telegram_session.)
        out = datt.send_discord_attachment(file_path=path, session_id="web:abc")
        assert out["status"] == "error" and "no target channel" in out["reason"]
    finally:
        os.remove(path)


def test_attach_dry_run_does_not_upload(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", True, raising=False)

    def _boom(*a, **k):
        raise AssertionError("upload must not happen during dry-run")

    monkeypatch.setattr(datt, "_upload_file", _boom)
    path = _tmp_file()
    try:
        out = datt.send_discord_attachment(file_path=path, channel_id="C")
        assert out["status"] == "ok" and out["dry_run"] is True
    finally:
        os.remove(path)


def test_attach_uploads_when_live(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False, raising=False)
    monkeypatch.setattr(
        datt,
        "_upload_file",
        lambda cid, p, cap: {
            "id": "m1",
            "attachments": [{"url": "https://cdn.discordapp.com/x.pdf"}],
        },
    )
    path = _tmp_file()
    try:
        out = datt.send_discord_attachment(file_path=path, channel_id="C", caption="hi")
        assert out["status"] == "ok"
        assert out["message_id"] == "m1"
        assert out["attachment_url"].endswith(".pdf")
    finally:
        os.remove(path)


def test_attach_upload_failure_reports_error(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False, raising=False)
    monkeypatch.setattr(datt, "_upload_file", lambda cid, p, cap: None)
    path = _tmp_file()
    try:
        out = datt.send_discord_attachment(file_path=path, channel_id="C")
        assert out["status"] == "error" and out["channel_id"] == "C"
    finally:
        os.remove(path)


# ── registration surfaces ────────────────────────────────────────────────


def test_tools_discovered_and_governor_gated():
    from app.tool_registry import discover_tools

    names = {s.name for s in discover_tools()}
    assert "post_discord_message" in names
    assert "send_discord_attachment" in names
    assert policy.classify_action("post_discord_message") == policy.ActionClass.WRITE
    assert policy.classify_action("send_discord_attachment") == policy.ActionClass.WRITE


def test_spec_handlers_return_error_json_without_channel():
    from app.tool_registry import get_registry

    spec = get_registry()["post_discord_message"]
    out = json.loads(spec.handler({"message": "hi"}, {"session_id": "tg:1:2"}))
    assert out["status"] == "error"
