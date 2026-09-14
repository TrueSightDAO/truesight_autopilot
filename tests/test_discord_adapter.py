"""Unit tests for the Discord adapter's pure logic + the security gate.

Mirrors tests/test_telegram_adapter.py: no network, no gateway — only the
pure helpers and the allowlist/identity gate (patched).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


def test_author_role_binding_not_governor(monkeypatch):
    monkeypatch.setattr(da, "discord_email", lambda uid: "random@example.com")
    monkeypatch.setattr(da, "_email_is_governor", lambda email: False)
    assert da.author_role("999", set()) == "guest"


def test_author_role_fail_closed_on_error(monkeypatch):
    def boom(uid):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(da, "discord_email", boom)
    assert da.author_role("999", set()) == "guest"


# ── dispatch gate (data/instruction boundary) ───────────────────────────


def _msg(user_id="999", content="hello", bot=False, channel="555"):
    return {
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


def test_handle_message_governor_dispatches_and_replies(monkeypatch):
    sent = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat", lambda text, sid, key: "reply!")
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


# ── credential resolution (shared loader) ───────────────────────────────


def _fake_sheet_service(rows):
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": rows
    }
    return svc


def test_resolve_sheets_credentials_uses_shared_loader(monkeypatch):
    # No JSON-in-env override -> the shared google_creds loader is the default.
    monkeypatch.delenv("GOOGLE_SHEETS_CREDENTIALS", raising=False)
    from app.tools import google_creds

    sentinel = object()
    seen = {}

    def fake_load(service_account_name=None, scopes=None):
        seen["name"] = service_account_name
        seen["scopes"] = scopes
        return sentinel

    monkeypatch.setattr(google_creds, "load_credentials", fake_load)
    assert da._resolve_sheets_credentials() is sentinel
    assert seen["scopes"] == da._SHEETS_SCOPES


def test_resolve_sheets_credentials_override_first(monkeypatch):
    # A valid JSON-in-env override is honoured without touching the loader.
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS", '{"type": "service_account"}')
    sentinel = object()

    class _FakeCreds:
        @staticmethod
        def from_service_account_info(info, scopes=None):
            return sentinel

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials",
        _FakeCreds,
        raising=False,
    )
    assert da._resolve_sheets_credentials() is sentinel


def test_fetch_discord_id_email_row_match_returns_col_d_email(monkeypatch):
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: object())
    row = [""] * 7
    row[3] = "matheus@example.com"  # D — email
    row[6] = "578258537957031951"  # G — Discord snowflake
    svc = _fake_sheet_service([["Name", "B", "C", "Email", "E", "F", "Discord ID"], row])
    with patch("googleapiclient.discovery.build", return_value=svc):
        assert (
            da._fetch_discord_id_email("578258537957031951") == "matheus@example.com"
        )


def test_fetch_discord_id_email_legacy_handle_does_not_bind(monkeypatch):
    # Column G may hold legacy handles; only exact snowflakes should bind.
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: object())
    row = [""] * 7
    row[3] = "legacy@example.com"  # D — email
    row[6] = "H4N5#0433"  # G — legacy handle, NOT a snowflake
    svc = _fake_sheet_service([row])
    with patch("googleapiclient.discovery.build", return_value=svc):
        assert da._fetch_discord_id_email("578258537957031951") is None


def test_fetch_discord_id_email_unbound_returns_none(monkeypatch):
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: object())
    row = [""] * 7
    row[3] = "someone@example.com"
    row[6] = "111111111111111111"
    svc = _fake_sheet_service([row])
    with patch("googleapiclient.discovery.build", return_value=svc):
        assert da._fetch_discord_id_email("999") is None


def test_fetch_discord_id_email_credentials_failure_returns_none(monkeypatch):
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: None)
    assert da._fetch_discord_id_email("578258537957031951") is None


def test_fetch_discord_id_email_sheets_error_returns_none(monkeypatch):
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: object())
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = RuntimeError(
        "permission denied"
    )
    with patch("googleapiclient.discovery.build", return_value=svc):
        assert da._fetch_discord_id_email("578258537957031951") is None


def test_author_role_guest_on_credentials_failure(monkeypatch):
    monkeypatch.setattr(da, "_resolve_sheets_credentials", lambda: None)
    da._binding_cache.clear()
    assert da.author_role("578258537957031951", set()) == "guest"


def test_discord_email_does_not_cache_failure(monkeypatch):
    da._binding_cache.clear()
    monkeypatch.setattr(da, "_lookup_discord_id_email", lambda uid: (False, None))
    assert da.discord_email("123") is None
    assert "123" not in da._binding_cache  # failures must not be cached


def test_discord_email_caches_successful_lookup(monkeypatch):
    da._binding_cache.clear()
    monkeypatch.setattr(da, "_lookup_discord_id_email", lambda uid: (True, "a@b.c"))
    assert da.discord_email("123") == "a@b.c"
    assert da._binding_cache["123"][1] == "a@b.c"

