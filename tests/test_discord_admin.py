"""Unit tests for the Discord admin tools (create/update/delete channel+category).

No network: ``discord_adapter._api`` is monkeypatched. We assert the
dry-run default never touches the API, the delete guard refuses without
``confirm``, and guild/session resolution precedence holds.
"""

from __future__ import annotations

import json

from app import discord_adapter as da
from app import policy
from app.tools import discord_admin as dadmin


# ── pure helpers ──────────────────────────────────────────────────────────


def test_channel_type_map():
    assert dadmin._CHANNEL_TYPES["category"] == 4
    assert dadmin._CHANNEL_TYPES["text"] == 0
    assert dadmin._type_name(4) == "category"
    assert dadmin._type_name(0) == "text"
    assert dadmin._type_name(999) == "type999"
    assert dadmin._type_name("bogus") == "unknown"


def test_guild_id_from_session():
    assert dadmin._guild_id_from_session("dc:111:222") == "111"
    assert dadmin._guild_id_from_session("seed:dc:111:222") == "111"
    assert dadmin._guild_id_from_session("tg:1:2") is None
    assert dadmin._guild_id_from_session(None) is None


def test_resolve_guild_id_precedence(monkeypatch):
    monkeypatch.setattr(dadmin.settings, "discord_guild_id", "CFG", raising=False)
    # explicit wins
    assert dadmin._resolve_guild_id("EXPLICIT", "dc:FROM:1") == "EXPLICIT"
    # session next
    assert dadmin._resolve_guild_id(None, "dc:FROM:1") == "FROM"
    # config last
    assert dadmin._resolve_guild_id(None, None) == "CFG"


def test_resolve_guild_id_none_when_unset(monkeypatch):
    monkeypatch.setattr(dadmin.settings, "discord_guild_id", "", raising=False)
    assert dadmin._resolve_guild_id(None, None) is None


def _boom(*a, **k):
    raise AssertionError("_api must not be called during dry-run")


# ── create ────────────────────────────────────────────────────────────────


def test_create_dry_run_does_not_call_api(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.create_discord_channel(name="hello", channel_type="text", guild_id="G")
    assert out["status"] == "ok"
    assert out["dry_run"] is True
    assert out["payload"] == {"name": "hello", "type": 0}


def test_create_category_dry_run_payload(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.create_discord_channel(
        name="Governance", channel_type="category", guild_id="G"
    )
    assert out["payload"] == {"name": "Governance", "type": 4}


def test_create_with_parent_category(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.create_discord_channel(
        name="gov", channel_type="text", category_id="CAT", guild_id="G"
    )
    assert out["payload"]["parent_id"] == "CAT"


def test_create_unknown_type():
    out = dadmin.create_discord_channel(name="x", channel_type="hologram", guild_id="G")
    assert out["status"] == "error"
    assert "unknown channel_type" in out["reason"]


def test_create_missing_name():
    out = dadmin.create_discord_channel(name="  ", guild_id="G")
    assert out["status"] == "error"


def test_create_no_guild(monkeypatch):
    monkeypatch.setattr(dadmin.settings, "discord_guild_id", "", raising=False)
    out = dadmin.create_discord_channel(name="x")
    assert out["status"] == "error"
    assert "no guild id" in out["reason"]


def test_create_executes(monkeypatch):
    seen = {}

    def fake_api(method, path, payload=None):
        seen["method"], seen["path"], seen["payload"] = method, path, payload
        return {"id": "123", "name": "hello", "type": 0, "parent_id": None}

    monkeypatch.setattr(da, "_api", fake_api)
    out = dadmin.create_discord_channel(
        name="hello", channel_type="text", guild_id="G", dry_run=False
    )
    assert out["status"] == "ok"
    assert out["action"] == "created"
    assert out["channel_id"] == "123"
    assert seen["method"] == "POST"
    assert seen["path"] == "/guilds/G/channels"


def test_create_api_failure(monkeypatch):
    monkeypatch.setattr(da, "_api", lambda *a, **k: None)
    out = dadmin.create_discord_channel(name="x", guild_id="G", dry_run=False)
    assert out["status"] == "error"
    assert "hint" in out


# ── update ────────────────────────────────────────────────────────────────


def test_update_dry_run_move_payload(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.update_discord_channel(channel_id="C", category_id="CAT")
    assert out["dry_run"] is True
    assert out["payload"] == {"parent_id": "CAT"}


def test_update_empty_category_moves_to_top(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.update_discord_channel(channel_id="C", category_id="")
    assert out["payload"] == {"parent_id": None}


def test_update_rename_and_position(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.update_discord_channel(channel_id="C", name="New", position=3)
    assert out["payload"] == {"name": "New", "position": 3}


def test_update_nothing_to_do(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.update_discord_channel(channel_id="C")
    assert out["status"] == "error"


def test_update_executes(monkeypatch):
    seen = {}

    def fake_api(method, path, payload=None):
        seen["method"], seen["path"], seen["payload"] = method, path, payload
        return {"id": "C", "name": "New", "type": 0, "parent_id": "CAT", "position": 1}

    monkeypatch.setattr(da, "_api", fake_api)
    out = dadmin.update_discord_channel(
        channel_id="C", category_id="CAT", dry_run=False
    )
    assert out["status"] == "ok"
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/channels/C"


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_dry_run_does_not_call_api(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.delete_discord_channel(channel_id="C")
    assert out["dry_run"] is True
    assert "irreversible" in out["message"]


def test_delete_refuses_without_confirm(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    out = dadmin.delete_discord_channel(channel_id="C", dry_run=False, confirm=False)
    assert out["status"] == "refused"


def test_delete_executes(monkeypatch):
    seen = {}

    def fake_api(method, path, payload=None):
        seen["method"], seen["path"] = method, path
        return {}

    monkeypatch.setattr(da, "_api", fake_api)
    out = dadmin.delete_discord_channel(channel_id="C", dry_run=False, confirm=True)
    assert out["status"] == "ok"
    assert out["action"] == "deleted"
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/channels/C"


# ── list ──────────────────────────────────────────────────────────────────


def test_list_tree(monkeypatch):
    tree = [
        {"id": "CAT1", "name": "Governance", "type": 4, "position": 0},
        {"id": "C1", "name": "gov", "type": 0, "position": 0, "parent_id": "CAT1"},
        {"id": "LOOSE", "name": "general", "type": 0, "position": 1},
    ]
    monkeypatch.setattr(da, "_api", lambda *a, **k: tree)
    out = dadmin.list_discord_channels(guild_id="G")
    assert out["status"] == "ok"
    assert out["counts"] == {"total": 3, "categories": 1, "channels": 2}
    assert "Governance" in out["tree"] and "gov" in out["tree"]


def test_list_no_guild(monkeypatch):
    monkeypatch.setattr(dadmin.settings, "discord_guild_id", "", raising=False)
    out = dadmin.list_discord_channels()
    assert out["status"] == "error"


# ── tool specs + policy ───────────────────────────────────────────────────


def test_tool_specs_names():
    names = {s.name for s in dadmin.TOOL_SPECS}
    assert names == {
        "list_discord_channels",
        "create_discord_channel",
        "update_discord_channel",
        "delete_discord_channel",
    }


def test_mutating_tools_are_governor_gated():
    for name in (
        "create_discord_channel",
        "update_discord_channel",
        "delete_discord_channel",
    ):
        assert policy.classify_action(name) == policy.ActionClass.WRITE
    assert policy.classify_action("list_discord_channels") == policy.ActionClass.READ


def test_registry_discovers_new_tools():
    from app.tool_registry import reset_registry_for_tests, get_registry

    reset_registry_for_tests()
    reg = get_registry()
    assert "create_discord_channel" in reg
    assert "delete_discord_channel" in reg


def test_handler_returns_json(monkeypatch):
    monkeypatch.setattr(da, "_api", _boom)
    spec = next(s for s in dadmin.TOOL_SPECS if s.name == "create_discord_channel")
    out = spec.handler({"name": "x", "guild_id": "G"}, {})
    assert json.loads(out)["dry_run"] is True
