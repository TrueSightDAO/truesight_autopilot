"""Unit tests for the Discord adapter's pure logic + the security gate.

Mirrors tests/test_telegram_adapter.py: no network, no gateway — only the
pure helpers and the allowlist/identity gate (patched).
"""

from __future__ import annotations

import httpx

from app import discord_adapter as da


# ── pure helpers ────────────────────────────────────────────────────────


def test_parse_allowed_ids():
    assert da.parse_allowed_ids("123,456;789") == {"123", "456", "789"}
    assert da.parse_allowed_ids("") == set()
    assert da.parse_allowed_ids(" 849324553221832794 , junk ") == {"849324553221832794"}
    # snowflakes stay STRINGS (must not be int-coerced / truncated)
    assert "849324553221832794" in da.parse_allowed_ids("849324553221832794")


def test_is_allowed():
    allowed = {"123"}
    assert da.is_allowed("123", allowed) is True
    assert da.is_allowed(123, allowed) is True  # int accepted, str-compared
    assert da.is_allowed("999", allowed) is False
    assert da.is_allowed("123", set()) is False  # empty allowlist never allows


def test_build_session_id():
    assert da.build_session_id("923008087315587072", "923012941937250375") == (
        "dc:923008087315587072:923012941937250375"
    )


def test_chunk_text_short_and_long():
    assert da.chunk_text("hi") == ["hi"]
    long = ("word " * 1000).strip()
    chunks = da.chunk_text(long, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks).replace(" ", "") == long.replace(" ", "")


def test_chunk_text_prefers_breaks():
    text = "a" * 1500 + "\n\n" + "b" * 1500
    chunks = da.chunk_text(text, limit=2000)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 1500  # split on the blank line, no mid-word cut


def test_strip_bot_mention():
    assert da.strip_bot_mention("<@123> hello", "123") == "hello"
    assert da.strip_bot_mention("<@!123> hello", "123") == "hello"
    assert (
        da.strip_bot_mention("hello (not a mention)", "123") == "hello (not a mention)"
    )


def test_is_mention():
    assert da.is_mention("<@123> hi", "123") is True
    assert da.is_mention("<@!123> hi", "123") is True
    assert da.is_mention("hi there", "123") is False


def test_gateway_intents_bits():
    # Message Content + Guild Messages + Guild Members must all be requested.
    assert da.GATEWAY_INTENTS & (1 << 15)  # MESSAGE_CONTENT
    assert da.GATEWAY_INTENTS & (1 << 9)  # GUILD_MESSAGES
    assert da.GATEWAY_INTENTS & (1 << 1)  # GUILD_MEMBERS


# ── identity gate ───────────────────────────────────────────────────────


def test_author_role_env_allowlist():
    assert da.author_role("123", {"123"}) == "governor"


def test_author_role_guest_when_unbound(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: None)
    assert da.author_role("999", set()) == "guest"


def test_author_role_sheet_binding_governor(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: "gary@truesight.me")
    monkeypatch.setattr(da, "_email_is_governor", lambda email: True)
    assert da.author_role("999", set()) == "governor"


def test_author_role_bound_non_governor_is_member(monkeypatch):
    # A contributor bound to a Discord id but NOT in the Governors cache
    # resolves to 'member' (verified, non-governor) -- not 'guest'.
    monkeypatch.setattr(da, "discord_email", lambda uid: "theus.reis.ssa@gmail.com")
    monkeypatch.setattr(da, "_email_is_governor", lambda email: False)
    assert da.author_role("578258537957031951", set()) == "member"


def test_author_role_env_member_allowlist(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: None)
    monkeypatch.setattr(da.settings, "discord_member_user_ids", "999,111")
    assert da.author_role("999", set()) == "member"
    assert da.author_role("222", set()) == "guest"


def test_author_role_unbound_empty_member_allowlist_is_guest(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: None)
    monkeypatch.setattr(da.settings, "discord_member_user_ids", "")
    assert da.author_role("999", set()) == "guest"


def test_author_role_governor_beats_member(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: "random@example.com")
    monkeypatch.setattr(da, "_email_is_governor", lambda email: False)
    monkeypatch.setattr(da.settings, "discord_member_user_ids", "999")
    assert da.author_role("999", {"999"}) == "governor"


def test_author_role_fail_closed_on_error(monkeypatch):
    def boom(uid):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(da, "discord_email", boom)
    assert da.author_role("999", set()) == "guest"


# ── dispatch gate (data/instruction boundary) ───────────────────────────


def _msg(user_id="999", content="hello", bot=False, channel="555"):
    return {
        "id": "msg1",
        "channel_id": channel,
        "content": content,
        "author": {"id": user_id, "username": "u", "bot": bot},
    }


def test_handle_message_ignores_bots(monkeypatch):
    called = {"sent": False}
    monkeypatch.setattr(da, "send_message", lambda *a, **k: called.update(sent=True))
    da.handle_message(_msg(bot=True), {"999"}, "KEY", "1", "42")
    assert called["sent"] is False


def test_handle_message_non_governor_logged_not_dispatched(monkeypatch):
    observed = {"n": 0}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "guest")
    monkeypatch.setattr(
        da, "log_observed_message", lambda *a, **k: observed.update(n=observed["n"] + 1)
    )
    monkeypatch.setattr(
        da,
        "call_chat",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched!")),
    )
    da.handle_message(_msg(), set(), "KEY", "1", "42")
    assert observed["n"] == 1  # logged as context


def test_handle_message_member_unmentioned_observed_only(monkeypatch):
    """A member's UNMENTIONED chatter is context only (PR3 keeps casual noise out)."""
    observed = {"n": 0}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    monkeypatch.setattr(
        da, "log_observed_message", lambda *a, **k: observed.update(n=observed["n"] + 1)
    )
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("member dispatched!")),
    )
    da.handle_message(_msg(), set(), "KEY", "1", "42")
    assert observed["n"] == 1  # recognised + logged, but not dispatched


def test_handle_message_guest_never_dispatched_even_when_mentioned(monkeypatch):
    """An unknown author is observed only, even with an explicit bot @-mention."""
    observed = {"n": 0}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "guest")
    monkeypatch.setattr(
        da, "log_observed_message", lambda *a, **k: observed.update(n=observed["n"] + 1)
    )
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guest dispatched!")),
    )
    da.handle_message(_msg(content="<@42> hey there"), set(), "KEY", "1", "42")
    assert observed["n"] == 1


def test_handle_message_member_mention_dispatches_read_only(monkeypatch):
    """A member who @-mentions the bot IS dispatched (PR3), asserted as member
    and attributed by its OWN name -- never silently elevated to governor."""
    sent = {}
    seen = {}

    def _fake_progress(channel_id, message, session_id, public_key, role, name):
        seen.update(role=role, name=name, msg=message)
        return ("member reply", False)

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    monkeypatch.setattr(da, "call_chat_with_progress", _fake_progress)
    monkeypatch.setattr(
        da, "send_message", lambda ch, txt: sent.update(ch=ch, txt=txt) or []
    )
    da.handle_message(
        _msg(content="<@42> what is our cacao yield this season?"),
        set(),
        "KEY",
        "1",
        "42",
    )
    assert seen["role"] == "member"  # never elevated to governor
    assert seen["name"] == "u"  # attributed by its own name
    assert sent["txt"] == "member reply"


def test_handle_message_member_mention_is_stripped_before_brain(monkeypatch):
    """The bot @-mention is stripped before the member's prompt reaches the brain."""
    seen = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda ch, msg, sid, pk, role, name: seen.update(msg=msg) or ("ok", False),
    )
    monkeypatch.setattr(da, "send_message", lambda ch, txt: [])
    da.handle_message(
        _msg(content="<@42> research amazon cacao"), set(), "KEY", "1", "42"
    )
    assert seen["msg"] == "research amazon cacao"


def test_handle_message_governor_dispatches_and_replies(monkeypatch):
    sent = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat", lambda *a, **k: "reply!")
    monkeypatch.setattr(
        da, "send_message", lambda ch, txt: sent.update(ch=ch, txt=txt) or []
    )
    da.handle_message(_msg(content="<@42> do a thing"), {"999"}, "KEY", "1", "42")
    assert sent["txt"] == "reply!"
    assert sent["ch"] == "555"


def test_handle_message_governor_without_key_warns(monkeypatch):
    sent = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "send_message", lambda ch, txt: sent.update(txt=txt) or [])
    da.handle_message(_msg(), {"999"}, None, "1", "42")
    assert "\u26a0" in sent["txt"]


# ── send_message dry-run ────────────────────────────────────────────────


def test_send_message_dry_run_does_not_post(monkeypatch):
    posted = {"n": 0}
    monkeypatch.setattr(da, "_api", lambda *a, **k: posted.update(n=posted["n"] + 1))
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    ids = da.send_message("555", "hello world")
    assert posted["n"] == 0
    assert ids == ["dry-run"]


# --- working-ack reaction + timeout messaging --------------------------


def test_api_treats_204_as_success(monkeypatch):
    class _Resp:
        status_code = 204
        content = b""
        text = ""

        def json(self):
            return {}

    monkeypatch.setattr(da, "_headers", lambda: {})
    monkeypatch.setattr(da.httpx, "request", lambda *a, **k: _Resp())
    assert da._api("PUT", "/x") == {}


def test_add_reaction_dry_run_does_not_call_api(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    monkeypatch.setattr(da, "_api", lambda *a, **k: calls.update(n=calls["n"] + 1))
    assert da.add_reaction("555", "msg1") is False
    assert calls["n"] == 0


def test_add_reaction_puts_encoded_emoji_when_live(monkeypatch):
    seen = {}
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "_api", lambda m, p, *a, **k: seen.update(m=m, p=p) or {})
    assert da.add_reaction("555", "msg1") is True
    assert seen["m"] == "PUT"
    assert seen["p"] == "/channels/555/messages/msg1/reactions/%E2%8F%B3/@me"


def test_remove_reaction_uses_delete(monkeypatch):
    seen = {}
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "_api", lambda m, p, *a, **k: seen.update(m=m, p=p) or {})
    assert da.remove_reaction("555", "msg1") is True
    assert seen["m"] == "DELETE"


def test_call_chat_timeout_message_is_actionable(monkeypatch):
    def boom(*a, **k):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(da.httpx, "post", boom)
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "t")
    msg = da.call_chat("hi", "s", "KEY")
    assert "unusually long" in msg
    assert "unreachable" not in msg


def test_call_chat_connection_error_still_says_unreachable(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(da.httpx, "post", boom)
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "t")
    assert "unreachable" in da.call_chat("hi", "s", "KEY")


def test_handle_message_governor_acks_reaction(monkeypatch):
    acked = {}

    def _ack(ch, mid, *a, **k):
        acked["ch"] = ch
        acked["mid"] = mid
        return True

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat", lambda *a, **k: "reply!")
    monkeypatch.setattr(da, "send_message", lambda ch, txt: [])
    monkeypatch.setattr(da, "remove_reaction", lambda *a, **k: True)
    monkeypatch.setattr(da, "add_reaction", _ack)
    da.handle_message(_msg(content="<@42> do a thing"), {"999"}, "KEY", "1", "42")
    assert acked["ch"] == "555"
    assert acked["mid"] == "msg1"


# ── trusted-bot carve-out (plan DISCORD_ENVOY_GOVERNOR_PARITY, PR1) ──────────


def test_handle_message_untrusted_bot_still_dropped(monkeypatch):
    """(a) Regression guard: a bot NOT on the trusted list is dropped, exactly
    as before this plan -- the security invariant is intact."""
    called = {"sent": False, "observed": 0}
    monkeypatch.setattr(da.settings, "discord_trusted_bot_ids", "111")
    monkeypatch.setattr(da, "send_message", lambda *a, **k: called.update(sent=True))
    monkeypatch.setattr(
        da,
        "log_observed_message",
        lambda *a, **k: called.update(observed=called["observed"] + 1),
    )
    da.handle_message(_msg(user_id="222", bot=True), {"222"}, "KEY", "1", "42")
    assert called["sent"] is False
    assert called["observed"] == 0  # never even reached role resolution


def test_handle_message_trusted_bot_not_sentinel_observed_only(monkeypatch):
    """(b) A TRUSTED bot that does NOT resolve to governor/sentinel proceeds to
    the context-only (observed) branch -- still no dispatch/reply. The trusted
    list grants only 'don't drop', never authority."""
    seen = {"sent": False, "observed": 0, "dispatched": False}
    monkeypatch.setattr(da.settings, "discord_trusted_bot_ids", "111")
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    monkeypatch.setattr(
        da,
        "log_observed_message",
        lambda *a, **k: seen.update(observed=seen["observed"] + 1),
    )
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: seen.update(dispatched=True) or ("x", False),
    )
    monkeypatch.setattr(da, "send_message", lambda *a, **k: seen.update(sent=True))
    da.handle_message(
        _msg(user_id="111", content="hi", bot=True), set(), "KEY", "1", "42"
    )
    assert seen["observed"] == 1
    assert seen["dispatched"] is False
    assert seen["sent"] is False


def test_handle_message_trusted_bot_sentinel_dispatches(monkeypatch):
    """(c) A trusted bot that DOES resolve to sentinel is dispatched -- the
    target end state (Envoy supervising on Discord)."""
    seen = {}
    monkeypatch.setattr(da.settings, "discord_trusted_bot_ids", "111")
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "sentinel")

    def _fake_progress(channel_id, message, session_id, public_key, role, name):
        seen.update(role=role, name=name)
        return ("sentinel reply", False)

    monkeypatch.setattr(da, "call_chat_with_progress", _fake_progress)
    monkeypatch.setattr(da, "send_message", lambda ch, txt: seen.update(txt=txt) or [])
    da.handle_message(
        _msg(user_id="111", content="hello", bot=True), set(), "KEY", "1", "42"
    )
    assert seen["role"] == "sentinel"
    assert seen["txt"] == "sentinel reply"


def test_handle_message_own_bot_id_never_trusted(monkeypatch):
    """(d) Our OWN bot id is never trusted, even if erroneously added to the
    trusted list -- defense in depth against a bot-to-bot self-echo loop."""
    called = {"sent": False}
    monkeypatch.setattr(da.settings, "discord_trusted_bot_ids", "42,111")
    monkeypatch.setattr(
        da, "author_role", lambda uid, allowed: "sentinel"
    )  # even if it *would* resolve
    monkeypatch.setattr(da, "send_message", lambda *a, **k: called.update(sent=True))
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: called.update(sent=True) or ("x", False),
    )
    da.handle_message(
        _msg(user_id="42", content="self", bot=True), set(), "KEY", "1", "42"
    )
    assert called["sent"] is False
