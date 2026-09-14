"""Guard (2026-09-14): the Discord adapter must show a "Sophia is typing..."
indicator for the duration of a turn.

Root cause this guards against: the adapter was built without ANY typing
indicator -- no call to Discord's ``POST /channels/{id}/typing`` endpoint
existed under any name. So a governor saw total silence during long turns
(``/chat-blocking`` can run tools + multiple LLM calls for up to ~180s).

Two invariants:
  1. The bubble must be *refreshed* -- Discord clears it after ~10s, so a
     single fire-and-forget call would fade mid-turn.
  2. It must *stop* when the turn ends, so the bubble clears the moment the
     reply lands (and the refresh thread never outlives the turn).
"""

from __future__ import annotations

import threading
import time

from app import discord_adapter as da


def _no_dry_run(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False)


def test_post_typing_hits_typing_endpoint(monkeypatch):
    """post_typing must POST the documented typing-trigger route."""
    calls: list[tuple] = []

    def fake_api(method, path, payload=None):
        calls.append((method, path, payload))
        return {}

    monkeypatch.setattr(da, "_api", fake_api)
    da.post_typing("12345")

    assert calls == [("POST", "/channels/12345/typing", None)]


def test_typing_indicator_refreshes_then_stops(monkeypatch):
    """Bubble must be re-triggered periodically and stop on exit."""
    _no_dry_run(monkeypatch)
    hits: list[str] = []
    monkeypatch.setattr(da, "post_typing", lambda cid: hits.append(str(cid)))

    with da.TypingIndicator("999", interval=0.05):
        time.sleep(0.18)  # ~3 intervals
        assert hits, "indicator posted nothing while the turn ran"

    n_at_exit = len(hits)
    assert n_at_exit >= 2, f"expected refreshes, saw {n_at_exit}"
    time.sleep(0.15)
    assert len(hits) == n_at_exit, "indicator kept firing after stop()"


def test_typing_indicator_joins_thread(monkeypatch):
    """stop() must not leak the refresh thread."""
    _no_dry_run(monkeypatch)
    monkeypatch.setattr(da, "post_typing", lambda cid: None)

    ind = da.TypingIndicator("1", interval=0.05)
    ind.start()
    ind.stop()

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if "dc-typing" not in {t.name for t in threading.enumerate()}:
            break
        time.sleep(0.02)
    assert "dc-typing" not in {t.name for t in threading.enumerate()}


def test_typing_indicator_silent_in_dry_run(monkeypatch):
    """Dry-run posts nothing, so it must show no bubble (and start no thread)."""
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    hits: list[str] = []
    monkeypatch.setattr(da, "post_typing", lambda cid: hits.append(str(cid)))

    with da.TypingIndicator("1", interval=0.05):
        time.sleep(0.12)
    assert hits == [], "dry-run must not trigger the typing endpoint"


def test_turn_wraps_call_chat_with_typing(monkeypatch):
    """The governor turn must hold the bubble around call_chat + send."""
    events: list[str] = []

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "resolve_governor_public_key", lambda: "pk")
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: (events.append("chat"), ("hi", True))[1],
    )
    monkeypatch.setattr(da, "send_message", lambda *a, **k: events.append("send") or [])

    class _Spy:
        def __init__(self, channel_id, *a, **k):
            events.append("typing-start")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append("typing-stop")

    monkeypatch.setattr(da, "TypingIndicator", _Spy)

    data = {
        "author": {"id": "849324553221832794", "username": "gary"},
        "channel_id": "42",
        "content": "hello",
    }
    da.handle_message(data, {"849324553221832794"}, "pk", "guild", "bot")

    # The turn now streams via call_chat_with_progress, which renders the reply
    # itself (edit-in-place) -> `shown=True`, so handle_message does NOT re-post.
    # `send_message` is not called on this path.
    assert events == ["typing-start", "chat", "typing-stop"]
