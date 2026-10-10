"""Hermetic tests for the calendar->Discord bridge (pure helpers + config).

No network, no credentials: exercises the native-event payload mapper and the
digest Discord-post gate by monkeypatching the adapter + settings.
"""

from datetime import datetime, timedelta, timezone

from app import calendar_watchdog as cw


def _ev(**kw):
    base = {
        "id": "ev1",
        "summary": "DAO call",
        "location": "Zoom",
        "start": (datetime.now(timezone.utc) + timedelta(hours=10))
        .replace(microsecond=0)
        .isoformat(),
        "end": None,
        "htmlLink": "https://calendar.example/ev1",
        "attendees": [],
    }
    base.update(kw)
    return base


def test_native_payload_uses_start_and_defaults_end_to_1h():
    p = cw._native_event_payload(_ev())
    assert p is not None
    start = datetime.fromisoformat(p["start_iso"])
    end = datetime.fromisoformat(p["end_iso"])
    assert end - start == timedelta(hours=1)
    assert p["name"] == "DAO call"
    assert p["location"] == "Zoom"
    assert p["description"] == "https://calendar.example/ev1"


def test_native_payload_none_without_start():
    assert cw._native_event_payload({"id": "x", "summary": "no time"}) is None


def test_native_payload_clamps_overlong_fields():
    p = cw._native_event_payload(_ev(summary="X" * 500, location="L" * 500))
    assert len(p["name"]) == 100
    assert len(p["location"]) == 100


def test_native_payload_normalises_to_utc():
    p = cw._native_event_payload(_ev(start="2026-10-15T09:00:00-07:00"))
    assert p["start_iso"].endswith("+00:00")


def test_sync_native_events_is_idempotent(monkeypatch, tmp_path):
    calls = []

    class FakeAdapter:
        @staticmethod
        def create_scheduled_event(**kw):
            calls.append(kw)
            return {"id": f"d{len(calls)}"}

    import sys
    import types

    fake = types.ModuleType("app.discord_adapter")
    fake.create_scheduled_event = FakeAdapter.create_scheduled_event
    monkeypatch.setitem(sys.modules, "app.discord_adapter", fake)
    monkeypatch.setattr(cw, "_native_state_path", lambda: tmp_path / "s.json")

    ev = _ev()
    assert cw.sync_native_events([ev]) == 1
    # second pass: same event id -> no duplicate
    assert cw.sync_native_events([ev]) == 0
    assert len(calls) == 1


def test_sync_skips_events_needing_no_attention(monkeypatch, tmp_path):
    calls = []

    class FakeAdapter:
        @staticmethod
        def create_scheduled_event(**kw):
            calls.append(kw)
            return {"id": "d1"}

    import sys
    import types

    fake = types.ModuleType("app.discord_adapter")
    fake.create_scheduled_event = FakeAdapter.create_scheduled_event
    monkeypatch.setitem(sys.modules, "app.discord_adapter", fake)
    monkeypatch.setattr(cw, "_native_state_path", lambda: tmp_path / "s.json")

    far = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    assert cw.sync_native_events([_ev(start=far)]) == 0
    assert calls == []


def test_post_digest_discord_gated_off(monkeypatch):
    monkeypatch.setattr(cw.settings, "calendar_watch_discord_channel_id", "123")
    monkeypatch.setattr(cw.settings, "calendar_watch_enable_posts", False)
    import asyncio

    assert asyncio.run(cw._post_digest_discord("hi")) is False


def test_post_digest_discord_no_channel(monkeypatch):
    monkeypatch.setattr(cw.settings, "calendar_watch_discord_channel_id", "")
    import asyncio

    assert asyncio.run(cw._post_digest_discord("hi")) is False


def test_post_digest_discord_posts_when_enabled(monkeypatch):
    sent = []

    import sys
    import types

    fake = types.ModuleType("app.discord_adapter")
    fake.send_message = lambda ch, txt: sent.append((ch, txt)) or ["m1"]
    monkeypatch.setitem(sys.modules, "app.discord_adapter", fake)
    monkeypatch.setattr(cw.settings, "calendar_watch_discord_channel_id", "999")
    monkeypatch.setattr(cw.settings, "calendar_watch_enable_posts", True)
    import asyncio

    assert asyncio.run(cw._post_digest_discord("digest!")) is True
    assert sent == [("999", "digest!")]
