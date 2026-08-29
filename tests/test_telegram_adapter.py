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
    monkeypatch.setattr(
        ta,
        "send_typing",
        lambda *a, **k: typing_calls.__setitem__("n", typing_calls["n"] + 1),
    )
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
    # call_chat short-circuits to a "restarting" message unless the brain is up;
    # in this hermetic test there is no brain, so force the readiness gate open.
    monkeypatch.setattr(ta, "_wait_for_brain", lambda: True)

    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(
            200, json={"response": "  \n "}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    out = ta.call_chat("q", "tg:1:0", "PK")
    assert out.strip() != "" and "empty response" in out.lower()


def test_call_chat_with_progress_surfaces_error_event(monkeypatch):
    # When the /chat stream raises (e.g. a malformed-history 400) it emits an
    # `error` event and no `done`. The adapter must surface that error text, NOT
    # the bare "empty response" banner (the 2026-06-16 thread-5712 regression).
    monkeypatch.setattr(ta, "create_jwt", lambda pk: "tok")
    monkeypatch.setattr(ta, "_wait_for_brain", lambda: True)
    monkeypatch.setattr(ta, "send_message", lambda *a, **k: 999)  # status_id
    monkeypatch.setattr(ta, "edit_message_text", lambda *a, **k: True)
    monkeypatch.setattr(ta, "delete_message", lambda *a, **k: None)

    class _FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            yield (
                'data: {"type": "error", "content": "DeepseekException - Messages '
                "with role 'tool' must be a response to a preceding message with "
                'tool_calls"}'
            )

    monkeypatch.setattr(ta.httpx, "stream", lambda *a, **k: _FakeStream())
    text, displayed = ta.call_chat_with_progress(1, None, "status?", "tg:1:5712", "PK")
    assert displayed is True
    assert "empty response" not in text.lower()
    assert "error" in text.lower() and "self-heal" in text.lower()
    assert "DeepseekException" in text


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
        lambda chat_id, text, thread_id=None: calls.append(
            {"chat_id": chat_id, "text": text, "thread_id": thread_id}
        ),
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
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(
                chat_id=chat_id,
                thread_id=thread_id,
                message=message,
                session_id=session_id,
                public_key=public_key,
            )
            or ("", True)
        ),
    )
    # real forum topic (is_topic_message=True) => threaded session + threaded routing
    ta.handle_message(
        _msg(
            user_id=111, chat_id=555, text="what shipped?", thread_id=7, is_topic=True
        ),
        allowed={111},
        public_key="PK",
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
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(session_id=session_id, thread_id=thread_id) or ("", True)
        ),
    )
    ta.handle_message(
        _msg(user_id=111, chat_id=555, text="hi", thread_id=4242),
        allowed={111},
        public_key="PK",
    )
    assert captured["session_id"] == "tg:555:0"
    assert captured["thread_id"] is None


def test_handle_message_photo_routes_with_path(monkeypatch, sent):
    # B4: a photo message downloads the file and injects its path for the QR/fs tools
    monkeypatch.setattr(
        ta, "download_telegram_file", lambda fid: "/tmp/tg_attachments/x.jpg"
    )
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(message=message) or ("", True)
        ),
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
    monkeypatch.setattr(
        ta, "call_chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "x"
    )
    ta.handle_message(_msg(user_id=111, text="/help"), allowed={111}, public_key="PK")
    assert called["n"] == 0
    assert sent and "private DAO assistant" in sent[0]["text"]


# ── _handle_voice_reply: no duplicate text (regression for #208) ──


@pytest.fixture
def voice_stubs(monkeypatch):
    """Stub the voice/network side-effects; capture send_message text payloads."""
    sends: list[str] = []
    monkeypatch.setattr(
        ta, "send_message", lambda chat_id, text, thread_id=None: sends.append(text)
    )
    monkeypatch.setattr(ta, "send_voice_action", lambda *a, **k: None)
    monkeypatch.setattr(ta, "send_voice", lambda *a, **k: True)
    monkeypatch.setattr(
        ta, "synthesize_voice", lambda text, language="en": "/tmp/x.mp3"
    )
    monkeypatch.setattr(ta, "detect_language", lambda text: "en")
    return sends


def test_voice_reply_no_duplicate_text_when_already_shown(voice_stubs):
    # Progress path already displayed the answer → voice path must NOT resend it.
    ta._handle_voice_reply(
        555, None, "Here is the answer.", None, text_already_sent=True
    )
    assert voice_stubs == []  # voice sent, but no text resend


def test_voice_reply_links_only_followup_when_already_shown(voice_stubs):
    # With URLs, send a links-only follow-up — never the full response again.
    ta._handle_voice_reply(
        555, None, "See https://x.com/foo for details.", None, text_already_sent=True
    )
    assert len(voice_stubs) == 1
    assert "https://x.com/foo" in voice_stubs[0]
    assert "See https://x.com/foo for details." not in voice_stubs[0]


def test_voice_reply_sends_full_text_when_not_shown(voice_stubs):
    # Progress fallback returned text without displaying it → send the full text.
    ta._handle_voice_reply(555, None, "Fallback answer.", None, text_already_sent=False)
    assert voice_stubs == ["Fallback answer."]


# ── /verify identity on-ramp (Phase 1) ──


def _dm(user_id=222, chat_id=222, text="hi", username="garyjob"):
    return {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "username": username},
        "text": text,
    }


@pytest.fixture(autouse=True)
def _clear_pending():
    # Reset the global governor cache around each test so resolve_identity()
    # calls in the gate don't leak cached env state into other test modules.
    from app.policy import refresh_governor_cache

    refresh_governor_cache()
    ta._pending_verifications.clear()
    yield
    ta._pending_verifications.clear()
    refresh_governor_cache()


def test_verify_command_starts_challenge(monkeypatch, sent):
    monkeypatch.setattr(
        "app.identity_binding.mint_challenge",
        lambda email, telegram_id=None: {"success": True},
    )
    # user 222 is NOT in the allowlist, but /verify in a DM is the on-ramp.
    ta.handle_message(
        _dm(user_id=222, text="/verify gary@truesight.me"),
        allowed={111},
        public_key="PK",
    )
    assert 222 in ta._pending_verifications
    assert ta._pending_verifications[222]["email"] == "gary@truesight.me"
    assert any("code" in c["text"].lower() for c in sent)


def test_verify_command_rejects_bad_email(monkeypatch, sent):
    called = {"n": 0}
    monkeypatch.setattr(
        "app.identity_binding.mint_challenge",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"success": True},
    )
    ta.handle_message(
        _dm(user_id=222, text="/verify notanemail"), allowed={111}, public_key="PK"
    )
    assert called["n"] == 0
    assert 222 not in ta._pending_verifications
    assert any("usage" in c["text"].lower() for c in sent)


def test_verify_code_binds(monkeypatch, sent):
    monkeypatch.setattr(
        "app.identity_binding.consume_challenge",
        lambda email, code, tid, uname=None: {"success": True},
    )
    ta._pending_verifications[222] = {"email": "gary@truesight.me", "ts": time.time()}
    ta.handle_message(_dm(user_id=222, text="ABCDEFGH"), allowed={111}, public_key="PK")
    assert 222 not in ta._pending_verifications
    assert any("verified" in c["text"].lower() for c in sent)


def test_verify_wrong_code_keeps_pending(monkeypatch, sent):
    monkeypatch.setattr(
        "app.identity_binding.consume_challenge",
        lambda email, code, tid, uname=None: {
            "success": False,
            "error": "Invalid code.",
        },
    )
    ta._pending_verifications[222] = {"email": "gary@truesight.me", "ts": time.time()}
    ta.handle_message(_dm(user_id=222, text="WRONG999"), allowed={111}, public_key="PK")
    assert 222 in ta._pending_verifications  # still pending; can retry
    assert any("invalid" in c["text"].lower() for c in sent)


def test_verify_cancel(sent):
    ta._pending_verifications[222] = {"email": "gary@truesight.me", "ts": time.time()}
    ta.handle_message(_dm(user_id=222, text="/cancel"), allowed={111}, public_key="PK")
    assert 222 not in ta._pending_verifications
    assert any("cancel" in c["text"].lower() for c in sent)


def test_verify_only_in_dm_not_group(monkeypatch, sent):
    called = {"n": 0}
    monkeypatch.setattr(
        "app.identity_binding.mint_challenge",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"success": True},
    )
    monkeypatch.setattr(
        "app.identity_binding.check_binding_status", lambda tid: {"bound": False}
    )
    # /verify in a GROUP from a non-allowlisted user → no verify, gate rejects.
    ta.handle_message(
        {
            "chat": {"id": 555, "type": "group"},
            "from": {"id": 999},
            "text": "/verify x@y.com",
        },
        allowed={111},
        public_key="PK",
    )
    assert called["n"] == 0
    assert any("not authorized" in c["text"].lower() for c in sent)


def test_unbound_user_dm_still_rejected(monkeypatch, sent):
    # A normal DM (not /verify) from an unbound non-governor is still rejected —
    # the verify on-ramp does not open the gate to general chat.
    monkeypatch.setattr(
        "app.identity_binding.check_binding_status", lambda tid: {"bound": False}
    )
    from app.policy import refresh_governor_cache

    refresh_governor_cache()
    ta.handle_message(
        _dm(user_id=999, text="hello there"), allowed={111}, public_key="PK"
    )
    assert any("not authorized" in c["text"].lower() for c in sent)


def test_verified_governor_admitted_through_gate(monkeypatch, sent):
    # A non-allowlisted id that resolves to GOVERNOR via binding is admitted:
    # it reaches the chat path (call_chat_with_progress), not the reject.
    from app.policy import Identity, Role

    monkeypatch.setattr(
        "app.policy.resolve_identity",
        lambda **k: Identity(telegram_id=777, role=Role.GOVERNOR, name="Gary Teh"),
    )
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(hit=True) or ("", True)
        ),
    )
    monkeypatch.setattr(ta, "_handle_voice_reply", lambda *a, **k: None)
    ta.handle_message(
        _dm(user_id=777, text="what shipped?"), allowed={111}, public_key="PK"
    )
    assert captured.get("hit") is True


# ── Group mention-gating (2026-08-28) ──


def _group_msg(user_id=111, chat_id=555, text="hello", entities=None, reply_from=None):
    m = {
        "chat": {"id": chat_id, "type": "group"},
        "from": {"id": user_id, "username": "someuser"},
        "text": text,
    }
    if entities:
        m["entities"] = entities
    if reply_from:
        m["reply_to_message"] = {"from": {"username": reply_from}}
    return m


@pytest.fixture(autouse=True)
def _reset_mention_gating_caches():
    ta._own_username_cache = None
    ta._member_count_cache.clear()
    yield
    ta._own_username_cache = None
    ta._member_count_cache.clear()


def test_bot_was_mentioned_true_on_entity_match(monkeypatch):
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    msg = _group_msg(
        text="@nelanco_bot hi there",
        entities=[{"type": "mention", "offset": 0, "length": 12}],
    )
    assert ta._bot_was_mentioned(msg) is True


def test_bot_was_mentioned_false_on_other_mention(monkeypatch):
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    msg = _group_msg(
        text="@someone_else hi there",
        entities=[{"type": "mention", "offset": 0, "length": 13}],
    )
    assert ta._bot_was_mentioned(msg) is False


def test_bot_was_mentioned_true_on_reply_to_own_message(monkeypatch):
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    msg = _group_msg(text="ok thanks", reply_from="nelanco_bot")
    assert ta._bot_was_mentioned(msg) is True


def test_bot_was_mentioned_false_with_no_signal(monkeypatch):
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    msg = _group_msg(text="just chatting")
    assert ta._bot_was_mentioned(msg) is False


def test_should_always_respond_private_chat():
    assert ta._should_always_respond("private", 555) is True


def test_should_always_respond_two_person_group(monkeypatch):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 2)
    assert ta._should_always_respond("group", 555) is True


def test_should_always_respond_large_group(monkeypatch):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 12)
    assert ta._should_always_respond("supergroup", 555) is False


def test_should_always_respond_fails_open_on_unknown_count(monkeypatch):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: None)
    assert ta._should_always_respond("group", 555) is True


def test_get_member_count_caches(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return httpx.Response(
            200, json={"result": 7}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(ta.httpx, "get", fake_get)
    assert ta._get_member_count(555) == 7
    assert ta._get_member_count(555) == 7
    assert calls["n"] == 1  # second call hit the cache


def test_log_observed_message_posts_to_chat_observe(monkeypatch):
    monkeypatch.setattr(ta, "create_jwt", lambda pk: "tok")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"status": "logged"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ta.httpx, "post", fake_post)
    ta.log_observed_message("just chatting", "tg:555:0", "PK", "Alice")
    assert captured["url"].endswith("/chat/observe")
    assert captured["json"] == {"message": "just chatting", "sender_name": "Alice"}
    assert captured["headers"]["X-Session-Id"] == "tg:555:0"


def test_handle_message_large_group_unmentioned_logs_only(monkeypatch, sent):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 5)
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    logged = {}
    monkeypatch.setattr(
        ta,
        "log_observed_message",
        lambda message, session_id, public_key, sender_name: logged.update(
            message=message, session_id=session_id, sender_name=sender_name
        ),
    )
    called_chat = {"n": 0}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda *a, **k: called_chat.__setitem__("n", called_chat["n"] + 1) or ("", True),
    )
    ta.handle_message(
        _group_msg(user_id=111, chat_id=555, text="just chatting about lunch"),
        allowed={111},
        public_key="PK",
    )
    assert logged["message"] == "just chatting about lunch"
    assert logged["sender_name"] == "someuser"
    assert called_chat["n"] == 0
    assert sent == []  # no reply sent for unmentioned chatter


def test_handle_message_large_group_mentioned_calls_chat(monkeypatch, sent):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 5)
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    monkeypatch.setattr(ta, "log_observed_message", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not log-only when mentioned")
    ))
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(hit=True) or ("", True)
        ),
    )
    msg = _group_msg(
        user_id=111,
        chat_id=555,
        text="@nelanco_bot what's the status?",
        entities=[{"type": "mention", "offset": 0, "length": 12}],
    )
    ta.handle_message(msg, allowed={111}, public_key="PK")
    assert captured.get("hit") is True


def test_handle_message_two_person_group_always_responds(monkeypatch, sent):
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 2)
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    monkeypatch.setattr(ta, "log_observed_message", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not log-only in a 2-person group")
    ))
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(hit=True) or ("", True)
        ),
    )
    ta.handle_message(
        _group_msg(user_id=111, chat_id=555, text="no mention needed here"),
        allowed={111},
        public_key="PK",
    )
    assert captured.get("hit") is True


def test_handle_message_large_group_attachment_always_processed(monkeypatch, sent):
    # Attachments bypass the mention gate entirely — always full processing.
    monkeypatch.setattr(ta, "_get_member_count", lambda chat_id: 8)
    monkeypatch.setattr(ta, "_resolve_own_username", lambda: "nelanco_bot")
    monkeypatch.setattr(ta, "log_observed_message", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("attachments must not be gated")
    ))
    monkeypatch.setattr(
        ta, "download_telegram_file", lambda fid: "/tmp/tg_attachments/x.jpg"
    )
    captured = {}
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda chat_id, thread_id, message, session_id, public_key, **kwargs: (
            captured.update(hit=True) or ("", True)
        ),
    )
    msg = {
        "chat": {"id": 555, "type": "group"},
        "from": {"id": 111, "username": "someuser"},
        "photo": [{"file_id": "small"}, {"file_id": "big"}],
        "caption": "look at this",
    }
    ta.handle_message(msg, allowed={111}, public_key="PK")
    assert captured.get("hit") is True


# -- Emoji-reaction go-signal (PR1: parser + handler) --

import logging as _logging


def test_reaction_emoji_verdict_go_for_standard_emoji():
    v = ta.reaction_emoji_verdict
    assert v([{"type": "emoji", "emoji": "👍"}]) == "go"  # thumbs up
    assert v([{"type": "emoji", "emoji": "🔥"}]) == "go"  # fire
    assert v([{"type": "emoji", "emoji": "❤️"}]) == "go"  # heart


def test_reaction_emoji_verdict_blocked_thumbs_down():
    assert ta.reaction_emoji_verdict([{"type": "emoji", "emoji": "👎"}]) == "blocked"


def test_reaction_emoji_verdict_custom_emoji_ignored():
    # Custom (paid) emoji carry custom_emoji_id, not emoji -- never a go.
    assert (
        ta.reaction_emoji_verdict([{"type": "custom_emoji", "custom_emoji_id": "x"}])
        == "custom"
    )
    # A custom emoji alongside a standard go emoji still counts as go.
    assert (
        ta.reaction_emoji_verdict(
            [
                {"type": "custom_emoji", "custom_emoji_id": "x"},
                {"type": "emoji", "emoji": "👍"},
            ]
        )
        == "go"
    )


def test_reaction_emoji_verdict_none_on_empty():
    assert ta.reaction_emoji_verdict([]) == "none"
    assert ta.reaction_emoji_verdict(None) == "none"
    assert ta.reaction_emoji_verdict("junk") == "none"


def test_reaction_emoji_verdict_honors_blocked_override():
    assert (
        ta.reaction_emoji_verdict([{"type": "emoji", "emoji": "😀"}], blocked=["😀"])
        == "blocked"
    )


def test_get_updates_sends_allowed_updates(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return httpx.Response(
            200, json={"result": []}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    ta.get_updates(None)
    import json as _json

    assert _json.loads(captured["params"]["allowed_updates"]) == [
        "message",
        "edited_message",
        "callback_query",
        "message_reaction",
    ]


def test_handle_message_reaction_logs_go_verdict(monkeypatch, caplog):
    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    with caplog.at_level(_logging.INFO, logger="autopilot.telegram"):
        ta.handle_message_reaction(
            {
                "chat": {"id": -1003919341801, "type": "supergroup"},
                "message_id": 1234,
                "user": {"id": 2102593402, "username": "garyjob"},
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
            allowed={2102593402},
        )
    text = caplog.text
    assert "verdict=go" in text
    assert "authorized=True" in text
    assert "chat=-1003919341801" in text
    assert "msg=1234" in text


def test_handle_message_reaction_unauthorized_reactor(monkeypatch, caplog):
    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: False)
    with caplog.at_level(_logging.INFO, logger="autopilot.telegram"):
        ta.handle_message_reaction(
            {
                "chat": {"id": -1003919341801},
                "message_id": 1234,
                "user": {"id": 999},
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
            allowed={2102593402},
        )
    assert "verdict=go" in caplog.text
    assert "authorized=False" in caplog.text


def test_handle_message_reaction_incomplete_ignored(caplog):
    with caplog.at_level(_logging.INFO, logger="autopilot.telegram"):
        ta.handle_message_reaction({"user": {"id": 999}}, allowed={999})
    assert "incomplete update" in caplog.text


def test_reaction_reactor_authorized_allowlist():
    assert ta._reaction_reactor_authorized(111, {111}) is True
    assert ta._reaction_reactor_authorized(999, {111}) is False


# ── send_message: resume_awaiting flag captures EVERY chunk's message_id (PR2 §1.4) ──


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _send_ok(result):
    return _FakeResp(200, {"ok": True, "result": result})


def test_send_message_resume_awaiting_registers_all_chunks(monkeypatch):
    import app.resume_registry as rr

    calls = []
    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: (
            calls.append(json) or _send_ok({"message_id": 1000 + len(calls)})
        ),
    )
    # spy on the registry
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append((mid, tid))
    )
    # long text -> 2 chunks
    long_text = ("line\n" * 100) + "\n✅ Ready. Reply go for it." + ("y" * 300)
    mid = ta.send_message(-1001, long_text, thread_id=15728, resume_awaiting=True)
    assert mid == 1001  # first chunk id (backward compat)
    assert len(marked) == len(calls)  # EVERY chunk registered
    assert all(tid == 15728 for _, tid in marked)
    # the registry captures each distinct chunk message_id
    assert len({m for m, _ in marked}) == len(marked)


def test_send_message_auto_flags_resume_here_text(monkeypatch):
    """A post containing 'RESUME HERE' self-flags even without resume_awaiting=True."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({"message_id": 888}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append((mid, tid))
    )
    ta.send_message(-1001, "Turn report. 📌 RESUME HERE = next unit.", thread_id=15991)
    assert marked == [(888, 15991)]  # auto-flagged from text


def test_send_message_plain_text_also_flagged(monkeypatch):
    """Every posted message is resume-awaiting now (2026-08-29: dropped the
    RESUME HERE / resume_awaiting gate) — any positive emoji reaction on ANY
    of her messages should mean "continue", not just specially-marked ones."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({"message_id": 999}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append(mid)
    )
    ta.send_message(-1001, "All done, nothing to resume.", thread_id=15991)
    assert marked == [999]


def test_send_message_resume_awaiting_false_still_registers(monkeypatch):
    """resume_awaiting=False no longer opts out — flagging is unconditional now."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({"message_id": 777}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append(mid)
    )
    ta.send_message(-1001, "plain message", thread_id=15728, resume_awaiting=False)
    assert marked == [777]


# ── edit_message_text: RESUME HERE auto-flag (PR #336 — the edit path) ──


def test_edit_message_text_auto_flags_resume_here(monkeypatch):
    """A short turn-report is delivered by EDITING the status message — the edited
    text carrying '📌 RESUME HERE' must flag that message_id (same message,
    same id), so a 👍 on it triggers a resume."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append((mid, tid))
    )
    ok = ta.edit_message_text(-1001, 4242, "📌 RESUME HERE = next unit", 15991)
    assert ok is True
    assert (4242, 15991) in marked  # the EDITED message_id is what the reaction hits


def test_edit_message_text_plain_also_flagged(monkeypatch):
    """Every edited message is resume-awaiting too now (2026-08-29: dropped
    the RESUME HERE gate) — as long as it has a thread to resume into."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append(mid)
    )
    ta.edit_message_text(-1001, 4243, "Still working on it…", 15991)
    assert marked == [4243]


def test_edit_message_text_without_thread_not_flagged(monkeypatch):
    """No thread -> no registry entry (registry needs a thread to resume into)."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_api", lambda m: f"https://api.telegram.org/botX/{m}")
    monkeypatch.setattr(
        ta.httpx,
        "post",
        lambda url, json=None, timeout=None: _send_ok({}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append(mid)
    )
    ta.edit_message_text(-1001, 4244, "📌 RESUME HERE", None)
    assert marked == []


def _reaction(emoji="👍", user_id=111, message_id=9001):
    return {
        "chat": {"id": -1001, "type": "supergroup"},
        "message_id": message_id,
        "user": {"id": user_id, "is_bot": False},
        "date": 1700000000,
        "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": emoji}],
    }


def test_reaction_go_resumes_from_registry(monkeypatch):
    """Authorized go reaction on a resume-awaiting message -> enqueues a turn."""
    import app.resume_registry as rr

    resumed = {}
    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    monkeypatch.setattr(
        rr,
        "lookup",
        lambda mid: (
            {"thread_id": "15728", "text": "✅ Ready — reply go for it"}
            if mid == 9001
            else None
        ),
    )
    monkeypatch.setattr(ta, "resolve_governor_public_key", lambda: "PUBKEY")
    monkeypatch.setattr(
        ta,
        "_run_turn_with_auto_advance",
        lambda chat_id, thread_id, dispatch_text, session_id, public_key, is_voice, transcribed_text: (
            resumed.update(
                {
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "dispatch_text": dispatch_text,
                    "session_id": session_id,
                    "public_key": public_key,
                }
            )
        ),
    )
    ta.handle_message_reaction(_reaction("👍"), allowed={111})
    assert resumed["thread_id"] == 15728
    assert "[emoji-go: 👍 from user 111] go for it" in resumed["dispatch_text"]
    assert (
        "[Telegram context: chat_id=-1001, thread_id=15728]" in resumed["dispatch_text"]
    )


def test_reaction_go_does_not_deadlock_on_thread_lock(monkeypatch):
    """Regression test (2026-08-29): _maybe_resume_from_reaction used to wrap
    _run_turn_with_auto_advance() in its OWN `with lock:`, but that function
    acquires the SAME per-thread lock itself on every loop iteration -- a
    plain (non-reentrant) threading.Lock double-acquired from the same thread
    blocks forever, silently, with no exception. Every previous test in this
    file mocks _run_turn_with_auto_advance() entirely, so none of them could
    have caught this: this one exercises the REAL function (only mocking the
    innermost network call) and fails via timeout if the deadlock returns."""
    import threading

    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    monkeypatch.setattr(
        rr,
        "lookup",
        lambda mid: (
            {"thread_id": "15728", "text": "ready"} if mid == 9001 else None
        ),
    )
    monkeypatch.setattr(ta, "resolve_governor_public_key", lambda: "PUBKEY")
    monkeypatch.setattr(ta.settings, "auto_advance", False, raising=False)
    monkeypatch.setattr(
        ta,
        "call_chat_with_progress",
        lambda *a, **k: ("ok", True),
    )

    done = threading.Event()

    def run():
        ta.handle_message_reaction(_reaction("👍"), allowed={111})
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    finished = done.wait(timeout=5)
    assert finished, "handle_message_reaction deadlocked (see 2026-08-29 fix)"


def test_reaction_thumbsdown_not_a_go(monkeypatch):
    """👎 is explicitly NOT a go (decision 0.1) — nothing is enqueued."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    called = []
    monkeypatch.setattr(
        rr,
        "lookup",
        lambda mid: called.append(mid) or {"thread_id": "15728", "text": "x"},
    )
    monkeypatch.setattr(
        ta, "_run_turn_with_auto_advance", lambda *a, **k: called.append("RUN")
    )
    ta.handle_message_reaction(_reaction("👎"), allowed={111})
    assert "RUN" not in called  # lookup never even consulted for a blocked emoji


def test_reaction_on_non_resume_message_ignored(monkeypatch):
    """Reaction on a message NOT flagged resume-awaiting -> no resume (decision 0.2)."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    monkeypatch.setattr(rr, "lookup", lambda mid: None)  # not resume-awaiting
    monkeypatch.setattr(ta, "resolve_governor_public_key", lambda: "PUBKEY")
    ran = []
    monkeypatch.setattr(
        ta, "_run_turn_with_auto_advance", lambda *a, **k: ran.append(1)
    )
    ta.handle_message_reaction(_reaction("👍", message_id=7777), allowed={111})
    assert ran == []


def test_reaction_non_allowed_reactor_ignored(monkeypatch):
    """Non-allowlisted reactor -> no resume (decision 0.3)."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: False)
    monkeypatch.setattr(rr, "lookup", lambda mid: {"thread_id": "15728", "text": "x"})
    ran = []
    monkeypatch.setattr(
        ta, "_run_turn_with_auto_advance", lambda *a, **k: ran.append(1)
    )
    ta.handle_message_reaction(_reaction("👍", user_id=999), allowed={111})
    assert ran == []


def test_reaction_unusable_thread_dropped(monkeypatch):
    """Registry entry with no usable thread -> dropped, no turn."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    monkeypatch.setattr(rr, "lookup", lambda mid: {"thread_id": "", "text": "x"})
    ran = []
    monkeypatch.setattr(
        ta, "_run_turn_with_auto_advance", lambda *a, **k: ran.append(1)
    )
    ta.handle_message_reaction(_reaction("👍"), allowed={111})
    assert ran == []


def test_reaction_no_governor_identity_notifies(monkeypatch):
    """No governor identity configured -> notify in the recovered thread, no turn."""
    import app.resume_registry as rr

    monkeypatch.setattr(ta, "_reaction_reactor_authorized", lambda uid, allowed: True)
    monkeypatch.setattr(rr, "lookup", lambda mid: {"thread_id": "15728", "text": "x"})
    monkeypatch.setattr(ta, "resolve_governor_public_key", lambda: None)
    sent = []
    monkeypatch.setattr(
        ta, "send_message", lambda cid, text, tid=None: sent.append((cid, text, tid))
    )
    ran = []
    monkeypatch.setattr(
        ta, "_run_turn_with_auto_advance", lambda *a, **k: ran.append(1)
    )
    ta.handle_message_reaction(_reaction("👍"), allowed={111})
    assert ran == []
    assert any("No governor identity configured" in t for _, t, _ in sent)
