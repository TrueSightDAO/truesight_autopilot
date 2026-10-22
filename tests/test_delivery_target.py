"""Tests for platform-aware attachment routing (2026-10-22 wrong-destination incident)."""

from __future__ import annotations

import pytest

from app.tools.delivery_target import describe_destination, platform_from_session

PK = "MIIBIjANBgkqhkiG9w0B"
TG_KEY = f"{PK}:tg:-1003919341801:42426"
DC_KEY = f"{PK}:dc:923008087315587072:1562006554884972615"


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        (TG_KEY, "telegram"),
        (DC_KEY, "discord"),
        ("tg:-100:5", "telegram"),
        ("dc:1:2", "discord"),
        (None, None),
        ("", None),
        (PK, None),  # opaque key: no marker
        ("3567e80e7728", None),  # session hash: no marker
        (f"{TG_KEY}:{DC_KEY}", None),  # ambiguous -> refuse rather than guess
    ],
)
def test_platform_from_session(session_id, expected):
    assert platform_from_session(session_id) == expected


def test_describe_destination_telegram():
    d = describe_destination(TG_KEY)
    assert d["platform"] == "telegram"
    assert d["chat_id"] == "-1003919341801"
    assert d["thread_id"] == 42426


def test_describe_destination_discord():
    d = describe_destination(DC_KEY)
    assert d["platform"] == "discord"
    assert d["guild_id"] == "923008087315587072"
    assert d["channel_id"] == "1562006554884972615"


def test_describe_destination_never_raises():
    assert describe_destination(None)["platform"] is None
    assert describe_destination("garbage::")["platform"] is None


def test_telegram_tool_reroutes_on_discord_session(monkeypatch, tmp_path):
    """The incident: Discord session + telegram tool must NOT hit the home group."""
    from app.tools import discord_attachment, telegram_attachment

    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 test")

    seen = {}

    def fake_discord(**kwargs):
        seen.update(kwargs)
        return {"status": "ok", "channel_id": "1562006554884972615"}

    monkeypatch.setattr(discord_attachment, "send_discord_attachment", fake_discord)
    out = telegram_attachment.send_telegram_attachment(
        file_path=str(f), caption="hi", session_id=DC_KEY
    )
    assert out["status"] == "ok"
    assert out["routed_from"] == "send_telegram_attachment"
    assert seen["session_id"] == DC_KEY
    assert out["destination"]["platform"] == "discord"


def test_discord_tool_reroutes_on_telegram_session(monkeypatch, tmp_path):
    """Symmetric: Telegram session + discord tool must not hard-fail."""
    from app.tools import discord_attachment, telegram_attachment

    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    seen = {}

    def fake_tg(**kwargs):
        seen.update(kwargs)
        return {"status": "ok", "chat_id": "-1003919341801"}

    monkeypatch.setattr(telegram_attachment, "send_telegram_attachment", fake_tg)
    out = discord_attachment.send_discord_attachment(
        file_path=str(f), session_id=TG_KEY
    )
    assert out["status"] == "ok"
    assert out["routed_from"] == "send_discord_attachment"
    assert seen["session_id"] == TG_KEY


def test_explicit_id_beats_session_routing(monkeypatch, tmp_path):
    """An explicit channel_id must still win over session-derived routing."""
    from app.tools import discord_attachment, telegram_attachment

    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(
        telegram_attachment,
        "send_telegram_attachment",
        lambda **kw: pytest.fail("must not reroute when channel_id is explicit"),
    )
    out = discord_attachment.send_discord_attachment(
        file_path=str(f), channel_id="999", session_id=TG_KEY
    )
    # reaches the Discord path (dry-run or a real attempt), never the reroute
    assert out.get("channel_id") == "999"
