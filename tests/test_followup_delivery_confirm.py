"""Regression guards for the 2026-09-02 follow-up delivery bug.

The direct-HTTP Telegram fallback resent the raw markdown with ``parse_mode``
still ``HTML`` -> Telegram 400 "can't parse entities"; the strike ping never
landed, yet the loop resolved the follow-up anyway (silently eating
warmup-conversion-30day-readout, chocolate-subscription-phase2,
matheus-nota-fiscal-exportacao and podream-tech-followup the same day).

Two contracts are pinned here:
  * the 400 fallback escapes entities (and keeps the topic id when the topic
    still exists) instead of 400-ing again;
  * ``send_message(..., require_thread=True)`` returns ``None`` when delivery
    fell back to the group's top level, so the strike notifier cannot report
    success for a ping nobody saw in the thread.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app import telegram_adapter as ta


def _ok(url: str = "https://api.telegram.org/x") -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "result": {"message_id": 1}},
        request=httpx.Request("POST", url),
    )


def _err(status: int, description: str, url: str = "https://api.telegram.org/x"):
    return httpx.Response(
        status,
        json={"ok": False, "description": description},
        request=httpx.Request("POST", url),
    )


def test_send_message_escapes_entities_and_keeps_thread_on_parse_400(monkeypatch):
    """The parse-error fallback must escape entities, not resend raw HTML."""
    posts: list[dict] = []

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        posts.append(dict(json))
        if json.get("parse_mode") == "HTML" and "&" in json.get("text", ""):
            return _err(400, "Bad Request: can't parse entities", url)
        return _ok(url)

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    msg_id = ta.send_message(555, "A & B", thread_id=7, require_thread=True)

    assert msg_id == 1
    assert posts[0]["parse_mode"] == "HTML"
    assert posts[1]["text"] == "A &amp; B"  # entities escaped
    assert "parse_mode" not in posts[1]  # not re-declared as HTML
    assert posts[1]["message_thread_id"] == 7  # topic still alive -> keep it


def test_send_message_keeps_thread_but_omits_it_when_topic_missing(monkeypatch):
    """Keeping the topic id is what makes the ping land IN the thread."""
    posts: list[dict] = []

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        posts.append(dict(json))
        if "message_thread_id" in json:
            return _err(400, "Bad Request: message thread not found", url)
        return _ok(url)

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    ta.send_message(555, "hi", thread_id=999)

    assert "message_thread_id" in posts[0]
    assert "message_thread_id" not in posts[1]  # topic gone -> drop it


def test_send_message_require_thread_returns_none_when_thread_dropped(monkeypatch):
    """require_thread=True must NOT claim delivery for a top-level fallback."""

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        if "message_thread_id" in json:
            return _err(400, "Bad Request: message thread not found", url)
        return _ok(url)

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    # Required delivery -> undelivered, so the loop retries instead of resolving.
    assert ta.send_message(555, "hi", thread_id=999, require_thread=True) is None
    # Lenient default (every other caller) keeps the old best-effort behaviour.
    assert ta.send_message(555, "hi", thread_id=999) == 1


class TestPostToThreadDirectEscaping:
    @pytest.mark.asyncio
    async def test_escapes_entities_and_keeps_thread(self, monkeypatch):
        from app.config import settings
        from app.followup_loop import _post_to_thread_direct

        monkeypatch.setattr(settings, "telegram_bot_api_key", "tok")
        posts: list[dict] = []

        class _Resp:
            def __init__(self, status, body):
                self.status_code = status
                self.text = body

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, timeout=None):
                posts.append(dict(json))
                if json.get("parse_mode") == "HTML":
                    return _Resp(400, "Bad Request: can't parse entities")
                return _Resp(200, "ok")

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _Client())
        ok = await _post_to_thread_direct("-1003919341801", "9346", "A & B")

        assert ok is True
        assert posts[0]["parse_mode"] == "HTML"
        assert "parse_mode" not in posts[1]
        assert posts[1]["text"] == "A &amp; B"
        assert posts[1]["message_thread_id"] == 9346  # kept -> lands in topic

    @pytest.mark.asyncio
    async def test_returns_false_when_topic_missing(self, monkeypatch):
        from app.config import settings
        from app.followup_loop import _post_to_thread_direct

        monkeypatch.setattr(settings, "telegram_bot_api_key", "tok")

        class _Resp:
            def __init__(self, status, body):
                self.status_code = status
                self.text = body

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, timeout=None):
                if json.get("message_thread_id") is not None:
                    return _Resp(400, "Bad Request: message thread not found")
                return _Resp(200, "ok")

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _Client())
        ok = await _post_to_thread_direct("-1003919341801", "9346", "hi")

        assert ok is False  # landed OUTSIDE the topic -> undelivered


class TestNotifyRequiresInThreadDelivery:
    @pytest.mark.asyncio
    async def test_passes_require_thread_true(self):
        from app.followup_loop import _post_to_thread

        with patch("app.telegram_adapter.send_message", return_value=1) as m:
            ok = await _post_to_thread("-1003919341801", "9346", "hi")

        assert ok is True
        m.assert_called_once_with(-1003919341801, "hi", 9346, require_thread=True)
