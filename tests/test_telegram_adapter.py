"""Unit tests for the Telegram adapter's pure logic + the security gate (httpx mocked)."""
from __future__ import annotations

import pytest

from app import telegram_adapter as ta


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


def test_chunk_text_splits_long_on_newlines():
    block = ("line\n" * 2000)  # ~10k chars, well over 4096
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
    monkeypatch.setattr(ta, "send_message", lambda chat_id, text, thread_id=None: calls.append(
        {"chat_id": chat_id, "text": text, "thread_id": thread_id}))
    monkeypatch.setattr(ta, "send_typing", lambda *a, **k: None)
    return calls


def _msg(user_id=111, chat_id=555, text="hello", thread_id=None):
    m = {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}
    if thread_id:
        m["message_thread_id"] = thread_id
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
    captured = {}

    def fake_call(message, session_id, public_key):
        captured.update(message=message, session_id=session_id, public_key=public_key)
        return "the answer"

    monkeypatch.setattr(ta, "call_chat", fake_call)
    ta.handle_message(_msg(user_id=111, chat_id=555, text="what shipped?", thread_id=7),
                      allowed={111}, public_key="PK")
    assert captured["message"] == "what shipped?"
    assert captured["session_id"] == "tg:555:7"
    assert captured["public_key"] == "PK"
    assert sent and sent[0]["text"] == "the answer"
    assert sent[0]["thread_id"] == 7


def test_handle_message_help_no_chat_call(monkeypatch, sent):
    called = {"n": 0}
    monkeypatch.setattr(ta, "call_chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "x")
    ta.handle_message(_msg(user_id=111, text="/help"), allowed={111}, public_key="PK")
    assert called["n"] == 0
    assert sent and "private DAO assistant" in sent[0]["text"]
