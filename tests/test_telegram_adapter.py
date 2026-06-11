"""Unit tests for the Telegram adapter's pure logic + the security gate (httpx mocked)."""

from __future__ import annotations

import time

import httpx
import pytest

from app import telegram_adapter as ta


def test_markdown_to_telegram_html():
    h = ta.markdown_to_telegram_html
    assert h("## Upcoming Events") == "<b>Upcoming Events</b>"
    assert h("### 1. SF Tech Fest") == "<b>1. SF Tech Fest</b>"
    assert h("**Date**: June 12") == "<b>Date</b>: June 12"
    assert h("- Item one") == "• Item one"
    assert h("* Item two") == "• Item two"
    assert h("Use `code` here") == "Use <code>code</code> here"
    assert h("[link](https://x.com)") == '<a href="https://x.com">link</a>'
    # header containing bold must NOT produce nested <b><b> (Telegram 400s on it)
    assert h("### 1. **SF Tech Fest 2026**") == "<b>1. SF Tech Fest 2026</b>"
    assert "<b><b>" not in h("## **Heading**")


def test_markdown_to_telegram_html_escapes_and_codeblocks():
    h = ta.markdown_to_telegram_html
    # raw < > & in text must be escaped so Telegram HTML parses
    assert h("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    # fenced code becomes <pre> with escaped inner
    out = h('```json\n{"a": 1 < 2}\n```')
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "&lt;" in out and "@@TGCODE" not in out  # escaped + placeholder restored


def test_markdown_to_telegram_html_no_stray_placeholders():
    out = ta.markdown_to_telegram_html("text `one` and `two` and ```\nblock\n```")
    assert "@@TGCODE" not in out
    assert out.count("<code>") == 2 and "<pre>block</pre>" in out


def test_extract_attachment_file_id():
    f = ta.extract_attachment_file_id
    # photo: pick the largest (last) size
    assert f({"photo": [{"file_id": "small"}, {"file_id": "big"}]}) == "big"
    # document
    assert f({"document": {"file_id": "doc1"}}) == "doc1"
    # text-only message → no attachment
    assert f({"text": "hello"}) is None
    assert f({}) is None
    assert f({"photo": []}) is None


def test_call_chat_with_typing_refreshes_indicator(monkeypatch):
    typing_calls = {"n": 0}
    monkeypatch.setattr(ta, "send_typing", lambda *a, **k: typing_calls.__setitem__("n", typing_calls["n"] + 1))
    monkeypatch.setattr(ta, "_TYPING_INTERVAL", 0.05)

    def slow_call(message, session_id, public_key):
        time.sleep(0.22)  # spans several typing intervals
        return "done"

    monkeypatch.setattr(ta, "call_chat", slow_call)
    out = ta.call_chat_with_typing(123, None, "q", "tg:1:0", "PK")
    assert out == "done"
    assert typing_calls["n"] >= 2  # initial + at least one keep-alive refresh


# ── parse_allowed_ids ──


def test_parse_allowed_ids_variants():
    assert ta.parse_allowed_ids("123, 456 ;789") == {123, 456, 789}
    assert ta.parse_allowed_ids("") == set()
    assert ta.parse_allowed_ids("  ") == set()
    assert ta.parse_allowed_ids("abc, 12, x9") == {12}  # junk ignored
    assert ta.parse_allowed_ids("-100123") == {-100123}  # group ids can be negative


# ── is_allowed (the security gate) ──


def test_is_allowed_requires_configured_allowlist():
    # Empty allowlist => nobody is "allowed" (bootstrap path handles those separately)
    assert ta.is_allowed(123, set()) is False
    assert ta.is_allowed(123, {123}) is True
    assert ta.is_allowed(999, {123}) is False


# ── build_session_id (topic => context) ──


def test_build_session_id():
    assert ta.build_session_id(555, None) == "tg:555:0"
    assert ta.build_session_id(555, 42) == "tg:555:42"
    # distinct topics in the same chat => distinct sessions
    assert ta.build_session_id(555, 1) != ta.build_session_id(555, 2)


# ── chunk_text ──


def test_chunk_text_short_passthrough():
    assert ta.chunk_text("hello") == ["hello"]
    assert ta.chunk_text("") == ["(no response)"]


def test_chunk_text_whitespace_only_becomes_placeholder():
    # whitespace must NOT be sent (Telegram: "text must be non-empty")
    assert ta.chunk_text("   \n  ") == ["(no response)"]
    assert ta.chunk_text("\n") == ["(no response)"]


def test_call_chat_whitespace_response_falls_back(monkeypatch):
    monkeypatch.setattr(ta, "create_jwt", lambda pk: "tok")

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return httpx.Response(200, json={"response": "  \n "}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    out = ta.call_chat("q", "tg:1:0", "PK")
    assert out.strip() != "" and "empty response" in out.lower()


def test_chunk_text_splits_long_on_newlines():
    block = "line\n" * 2000  # ~10k chars, well over 4096
    chunks = ta.chunk_text(block)
    assert len(chunks) >= 2
    assert all(len(c) <= ta._MESSAGE_LIMIT for c in chunks)


def test_chunk_text_splits_without_newlines():
    chunks = ta.chunk_text("x" * 9000)
    assert len(chunks) == 3
    assert all(len(c) <= ta._MESSAGE_LIMIT for c in chunks)


# ── handle_message: gate behaviour (capture outbound sends) ──


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        ta,
        "send_message",
        lambda chat_id, text, thread_id=None: calls.append({"chat_id": chat_id, "text": text, "thread_id": thread_id}),
    )
    monkeypatch.setattr(ta, "send_typing", lambda *a, **k: None)
    return calls


def _msg(user_id=111, chat_id=555, text="hello", thread_id=None, is_topic=False):
    m = {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}
    if thread_id:
        m["message_thread_id"] = thread_id
    if is_topic:
        m["is_topic_message"] = True
    return m


def test_handle_message_bootstrap_reveals_id(sent):
    # empty allowlist => reveal sender id, never call chat
    ta.handle_message(_msg(user_id=42), allowed=set(), public_key="PK")
    assert len(sent) == 1
    assert "42" in sent[0]["text"]
    assert "TELEGRAM_ALLOWED_USER_IDS" in sent[0]["text"]


def test_handle_message_rejects_non_allowlisted(sent):
    ta.handle_message(_msg(user_id=999), allowed={111}, public_key="PK")
    assert sent and "Not authorized" in sent[0]["text"]


def test_handle_message_allowed_calls_chat(monkeypatch, sent):
    # handle_message now routes to call_chat_with_progress (which sends its own reply)
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key: captured.update(
            chat_id=chat_id, thread_id=thread_id, message=message, session_id=session_id, public_key=public_key
        ),
    )
    # real forum topic (is_topic_message=True) => threaded session + threaded routing
    ta.handle_message(
        _msg(user_id=111, chat_id=555, text="what shipped?", thread_id=7, is_topic=True), allowed={111}, public_key="PK"
    )
    assert "what shipped?" in captured["message"]
    assert captured["session_id"] == "tg:555:7"
    assert captured["thread_id"] == 7
    assert captured["public_key"] == "PK"


def test_handle_message_reply_thread_not_treated_as_topic(monkeypatch, sent):
    # thread_id present but is_topic_message False (a reply-thread) => no thread routing,
    # session falls back to :0 (avoids the 400 on threaded sends).
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key: captured.update(
            session_id=session_id, thread_id=thread_id
        ),
    )
    ta.handle_message(_msg(user_id=111, chat_id=555, text="hi", thread_id=4242), allowed={111}, public_key="PK")
    assert captured["session_id"] == "tg:555:0"
    assert captured["thread_id"] is None


def test_handle_message_photo_routes_with_path(monkeypatch, sent):
    # B4: a photo message downloads the file and injects its path for the QR/fs tools
    monkeypatch.setattr(ta, "download_telegram_file", lambda fid: "/tmp/tg_attachments/x.jpg")
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key: captured.update(message=message),
    )
    msg = {
        "chat": {"id": 555},
        "from": {"id": 111},
        "photo": [{"file_id": "small"}, {"file_id": "big"}],
        "caption": "scan this",
    }
    ta.handle_message(msg, allowed={111}, public_key="PK")
    assert "scan this" in captured["message"]
    assert "/tmp/tg_attachments/x.jpg" in captured["message"]
    assert "scan_qr_from_file" in captured["message"]


def test_send_message_retries_without_thread_on_400(monkeypatch):
    posts: list[dict] = []

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        posts.append(dict(json))  # snapshot — send_message mutates+reuses the dict
        # first attempt (with thread) 400s; retry (no thread) ok
        status = 400 if "message_thread_id" in json else 200
        body = {"ok": status == 200, "description": "message thread not found"}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    ta.send_message(555, "hello", thread_id=99999)
    assert len(posts) == 2
    assert "message_thread_id" in posts[0]
    assert "message_thread_id" not in posts[1]  # fallback dropped it


def test_handle_message_help_no_chat_call(monkeypatch, sent):
    called = {"n": 0}
    monkeypatch.setattr(ta, "call_chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "x")
    ta.handle_message(_msg(user_id=111, text="/help"), allowed={111}, public_key="PK")
    assert called["n"] == 0
    assert sent and "private DAO assistant" in sent[0]["text"]
