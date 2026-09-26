"""Tests for Discord reaction-based go-signal (Tier-2 parity #6).

Mirrors tests/test_telegram_adapter.py's reaction coverage: a standard-emoji
reaction from an AUTHORIZED governor on a message Sophia flagged resume-awaiting
must act as a go-signal, dispatched through the same turn path a typed go-signal
uses. Reactions are gated exactly like text go-signals (allowlist / verified
governor), and a blocked emoji (thumbs-down) must never resume.
"""

from __future__ import annotations

from app import discord_adapter as da
from app import discord_resume_registry as drr


# \u2500\u2500 verdict \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_verdict_thumbs_up_is_go():
    assert da.discord_reaction_verdict({"id": None, "name": "\U0001f44d"}) == "go"


def test_verdict_thumbs_down_is_blocked():
    assert da.discord_reaction_verdict({"id": None, "name": "\U0001f44e"}) == "blocked"


def test_verdict_custom_emoji_ignored():
    assert da.discord_reaction_verdict({"id": "12345", "name": "party"}) == "custom"


def test_verdict_empty_is_none():
    assert da.discord_reaction_verdict(None) == "none"
    assert da.discord_reaction_verdict({}) == "none"
    assert da.discord_reaction_verdict({"id": None, "name": ""}) == "none"


def test_reaction_intent_present():
    assert da.GATEWAY_INTENTS & da._INTENT_GUILD_MESSAGE_REACTIONS


# \u2500\u2500 authorization gate \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_reactor_authorized_only_for_governor(monkeypatch):
    monkeypatch.setattr(
        da, "author_role", lambda uid, allowed: "governor" if uid == "9" else "guest"
    )
    assert da._reaction_reactor_authorized("9", {"9"}) is True
    assert da._reaction_reactor_authorized("7", {"9"}) is False


def test_reactor_authorized_fail_closed_on_error(monkeypatch):
    def boom(uid, allowed):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(da, "author_role", boom)
    assert da._reaction_reactor_authorized("9", {"9"}) is False


# \u2500\u2500 registry \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_registry_mark_lookup_consumes(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    drr.mark_resume_awaiting("m1", "c1", "ready")
    assert drr.is_resume_awaiting("m1") is True
    assert drr.lookup("m1") == {"channel_id": "c1", "text": "ready"}
    assert drr.lookup("m1") is None  # consumed
    assert drr.is_resume_awaiting("m1") is False


def test_registry_ignores_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    drr.mark_resume_awaiting("dry-run", "c1", "x")
    assert drr.is_resume_awaiting("dry-run") is False


# \u2500\u2500 reaction handler end-to-end \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _event(user_id="9", channel="c1", message="m1", name="\U0001f44d", eid=None):
    return {
        "user_id": user_id,
        "channel_id": channel,
        "message_id": message,
        "emoji": {"id": eid, "name": name},
    }


def _patch_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")


def test_go_reaction_dispatches_turn(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    drr.mark_resume_awaiting("m1", "c1", "deploy it")
    seen = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da, "handle_message", lambda d, a, k, g, b: seen.update(d=d, a=a, k=k, g=g, b=b)
    )
    da.handle_reaction(_event(), {"9"}, "KEY", "guild1", "42")
    assert seen["d"]["content"].startswith(
        "[emoji-go: \U0001f44d from user 9] go for it"
    )
    assert "deploy it" in seen["d"]["content"]
    assert seen["k"] == "KEY"


def test_blocked_reaction_does_not_dispatch(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    drr.mark_resume_awaiting("m1", "c1", "deploy it")
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da,
        "handle_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched!")),
    )
    da.handle_reaction(_event(name="\U0001f44e"), {"9"}, "KEY", "guild1", "42")
    # thumbs-down is blocked AND the entry must remain unconsumed
    assert drr.is_resume_awaiting("m1") is True


def test_unauthorized_reaction_does_not_dispatch(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    drr.mark_resume_awaiting("m1", "c1", "deploy it")
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "guest")
    monkeypatch.setattr(
        da,
        "handle_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched!")),
    )
    da.handle_reaction(_event(user_id="7"), {"9"}, "KEY", "guild1", "42")
    assert drr.is_resume_awaiting("m1") is True  # not consumed


def test_reaction_from_bot_ignored(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    drr.mark_resume_awaiting("m1", "c1", "x")
    monkeypatch.setattr(
        da,
        "handle_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched!")),
    )
    da.handle_reaction(_event(user_id="42"), {"9"}, "KEY", "guild1", "42")
    assert drr.is_resume_awaiting("m1") is True


def test_go_reaction_on_non_awaiting_message_is_noop(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da,
        "handle_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched!")),
    )
    da.handle_reaction(_event(message="never-seen"), {"9"}, "KEY", "guild1", "42")


def test_go_reaction_without_key_warns(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    drr.mark_resume_awaiting("m1", "c1", "x")
    warned = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da, "send_message", lambda ch, t: warned.update(ch=ch, t=t) or []
    )
    da.handle_reaction(_event(), {"9"}, None, "guild1", "42")
    assert warned["ch"] == "c1"
    assert "cannot resume" in warned["t"]


def test_send_message_flags_resume_awaiting(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "_api", lambda m, p, payload=None: {"id": "NEW"})
    da.send_message("c1", "a reply")
    assert drr.is_resume_awaiting("NEW") is True


def test_edit_message_text_flags_resume_awaiting(tmp_path, monkeypatch):
    _patch_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "_api", lambda m, p, payload=None: {"id": "EDITED"})
    assert da.edit_message_text("c1", "EDITED", "final answer") is True
    assert drr.is_resume_awaiting("EDITED") is True


# ── per-channel dispatch lock (parity with Telegram _thread_dispatch_lock) ────


def test_channel_dispatch_lock_is_stable_and_per_channel():
    """Same channel -> same Lock object; different channel -> different Lock."""
    a1 = da._channel_dispatch_lock("c1")
    a2 = da._channel_dispatch_lock("c1")
    b = da._channel_dispatch_lock("c2")
    assert a1 is a2  # stable identity across calls
    assert a1 is not b  # isolated per channel
    # str/int for the same id resolve to the same lock (same session key)
    assert da._channel_dispatch_lock(555) is da._channel_dispatch_lock("555")


def test_handle_message_serializes_turns_same_channel(monkeypatch):
    """Two governor messages in ONE channel must never run overlapping turns.

    Regression guard for the divergence found 2026-09-14: without the lock the
    ThreadPoolExecutor (max_workers=8) ran 4 concurrent turns on the same
    ``dc:{guild}:{channel}`` session. Telegram serializes per (chat, thread);
    Discord must match.
    """
    import threading
    import time

    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def slow_turn(*_a, **_k):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.15)  # long enough that an unguarded overlap is certain
        with guard:
            state["active"] -= 1
        return "reply", True

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat_with_progress", slow_turn)
    monkeypatch.setattr(da, "add_reaction", lambda *a, **k: None)
    monkeypatch.setattr(da, "remove_reaction", lambda *a, **k: None)
    monkeypatch.setattr(da, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(da.settings, "discord_dry_run", False)

    class _NoTyping:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(da, "TypingIndicator", _NoTyping)

    def one(i):
        da.handle_message(
            {
                "id": f"m{i}",
                "channel_id": "CHAN",
                "content": f"<@42> go {i}",
                "author": {"id": "999", "username": "gary", "bot": False},
            },
            {"999"},
            "KEY",
            "1",
            "42",
        )

    threads = [threading.Thread(target=one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] == 1, f"turns overlapped (peak={state['peak']})"


def test_handle_message_parallel_across_channels(monkeypatch):
    """Lock is per channel: distinct channels still run concurrently."""
    import threading
    import time

    state = {"active": 0, "peak": 0}
    guard = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def slow_turn(*_a, **_k):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            barrier.wait()  # both must be inside at once, or this raises
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.1)
        with guard:
            state["active"] -= 1
        return "reply", True

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat_with_progress", slow_turn)
    monkeypatch.setattr(da, "add_reaction", lambda *a, **k: None)
    monkeypatch.setattr(da, "remove_reaction", lambda *a, **k: None)
    monkeypatch.setattr(da, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(da.settings, "discord_dry_run", False)

    class _NoTyping:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(da, "TypingIndicator", _NoTyping)

    def one(i, chan):
        da.handle_message(
            {
                "id": f"m{i}",
                "channel_id": chan,
                "content": f"<@42> go {i}",
                "author": {"id": "999", "username": "gary", "bot": False},
            },
            {"999"},
            "KEY",
            "1",
            "42",
        )

    threads = [
        threading.Thread(target=one, args=(0, "C_A")),
        threading.Thread(target=one, args=(1, "C_B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] == 2, "distinct channels should NOT serialize each other"
