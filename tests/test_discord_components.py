"""Tests for Discord resume-option BUTTONS (component go-signal).

Discord twin of the Telegram inline-keyboard option tests: a tap on a
``ro:<token>:<i>`` button must ACK the interaction within Discord's 3s deadline
FIRST, then consume the option set and dispatch a synthesized go-signal through
the same turn path a reaction uses. Single-fire; a non-decision 'Other' button
and an expired menu never dispatch.
"""

from __future__ import annotations

from app import discord_adapter as da
from app import discord_resume_registry as drr


# ── component builder ────────────────────────────────────────────────────────


def test_build_components_shape():
    rows = da._build_components("K7QM", ["Run unit 3", "Stop"])
    assert len(rows) == 1
    btns = rows[0]["components"]
    assert [b["custom_id"] for b in btns] == ["ro:K7QM:0", "ro:K7QM:1", "ro:K7QM:o"]
    assert btns[0]["label"].startswith("1. ")
    assert "just reply" in btns[-1]["label"]  # non-decision Other
    assert btns[-1]["style"] == 2  # Other is secondary; options are primary


def test_new_option_token_is_retype_safe():
    tok = da._new_option_token()
    assert len(tok) in (4, 6)
    assert all(c in da._TOKEN_ALPHABET for c in tok)


# ── ACK timing ───────────────────────────────────────────────────────────────


def _capture_api(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def fake_api(method, path, payload=None):
        calls.append((method, path, payload or {}))
        return {}

    monkeypatch.setattr(da, "_api", fake_api)
    return calls


def test_ack_interaction_dry_run_does_not_call_api(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    calls = _capture_api(monkeypatch)
    da.ack_interaction("42", "tok")
    assert calls == []


def test_ack_interaction_deferred_is_type6(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    calls = _capture_api(monkeypatch)
    da.ack_interaction("42", "tok")
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/interactions/42/tok/callback"
    assert body == {"type": 6}  # deferred -> edit the original later


def test_ack_interaction_with_content_is_type7(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    calls = _capture_api(monkeypatch)
    da.ack_interaction("42", "tok", "hi")
    assert calls[0][2] == {"type": 7, "data": {"content": "hi"}}


def test_edit_via_interaction_uses_application_id_not_bot_token(monkeypatch):
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    calls = _capture_api(monkeypatch)
    da._edit_via_interaction("APPID", "tok", "done")
    assert calls[0][1] == "/webhooks/APPID/tok/messages/@original"


# ── end-to-end handler ───────────────────────────────────────────────────────


def _component_event(sel="1", token="K7QM", message="m1", user="9", channel="c1"):
    return {
        "type": 3,
        "id": "INT1",
        "token": "itok",
        "application_id": "APPID",
        "channel_id": channel,
        "member": {"user": {"id": user}},
        "message": {"id": message},
        "data": {"custom_id": f"ro:{token}:{sel}"},
    }


def _patch_env(
    tmp_path, monkeypatch, *, role="governor", options=("Run unit 3", "Stop")
):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: role)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        da, "handle_message", lambda *a, **k: events.append(("dispatch", a[0]))
    )
    monkeypatch.setattr(
        da, "send_message", lambda *a, **k: events.append(("send", {"args": a}))
    )
    if options is not None:
        drr.mark_options("K7QM", "c1", list(options))
    drr.mark_resume_awaiting("m1", "c1", "choose one")
    return events


def test_tap_acks_before_dispatch_and_resumes(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    events = _patch_env(tmp_path, monkeypatch)
    da.handle_component_interaction(_component_event(sel="0"), {"9"}, "PK", "G", "BOT")
    # 1) ACK hit the callback endpoint
    assert calls[0][1] == "/interactions/INT1/itok/callback"
    # 2) it was ACKed BEFORE the (slow) turn was dispatched
    assert events and events[0][0] == "dispatch"
    # 3) the synthesized go-signal carries the chosen label
    assert 'resume-option: "Run unit 3"' in events[0][1]["content"]
    assert "go for it" in events[0][1]["content"]


def test_tap_is_single_fire(tmp_path, monkeypatch):
    _capture_api(monkeypatch)
    events = _patch_env(tmp_path, monkeypatch)
    da.handle_component_interaction(_component_event(sel="1"), {"9"}, "PK", "G", "BOT")
    da.handle_component_interaction(_component_event(sel="1"), {"9"}, "PK", "G", "BOT")
    dispatches = [e for e in events if e[0] == "dispatch"]
    assert len(dispatches) == 1  # second tap: menu already consumed


def test_tap_other_never_dispatches(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    events = _patch_env(tmp_path, monkeypatch)
    da.handle_component_interaction(_component_event(sel="o"), {"9"}, "PK", "G", "BOT")
    assert not [e for e in events if e[0] == "dispatch"]
    # the interaction message was flipped to "awaiting typed reply"
    assert any(
        "Awaiting" in str(c[2].get("content", ""))
        for c in calls
        if c[1].startswith("/webhooks")
    )


def test_tap_expired_menu_does_not_dispatch(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    events: list = []
    monkeypatch.setattr(da, "handle_message", lambda *a, **k: events.append(a))
    da.handle_component_interaction(_component_event(sel="0"), {"9"}, "PK", "G", "BOT")
    assert events == []
    assert any(
        "expired" in str(c[2].get("content", ""))
        for c in calls
        if c[1].startswith("/webhooks")
    )


def test_tap_unauthorized_does_not_dispatch(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    events = _patch_env(tmp_path, monkeypatch, role="guest")
    da.handle_component_interaction(_component_event(sel="0"), {"9"}, "PK", "G", "BOT")
    assert not [e for e in events if e[0] == "dispatch"]
    assert any(
        "Not authorized" in str(c[2].get("content", ""))
        for c in calls
        if c[1].startswith("/webhooks")
    )


def test_non_component_interaction_ignored(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    _patch_env(tmp_path, monkeypatch)
    ev = _component_event()
    ev["type"] = 2  # APPLICATION_COMMAND, not MESSAGE_COMPONENT
    da.handle_component_interaction(ev, {"9"}, "PK", "G", "BOT")
    assert calls == []


# ── send side ────────────────────────────────────────────────────────────────


def test_send_with_components_dry_run_posts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    calls = _capture_api(monkeypatch)
    out = da.send_message_with_components("c1", "choose", ["A", "B"])
    assert calls == []
    assert out["status"] == "ok" and out["dry_run"] is True


def test_send_with_components_registers_options(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_dc.json")
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(da, "_api", lambda m, p, payload=None: {"id": "555"})
    out = da.send_message_with_components("c1", "choose", ["A", "B"])
    assert out["message_id"] == "555"
    token = out["option_token"]
    assert drr.peek_options(token) == ["A", "B"]
    assert drr.is_resume_awaiting("555") is True
