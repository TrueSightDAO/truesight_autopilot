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
