"""Hermetic tests for the deploy-complete -> Discord mirror (2026-10-10).

No network: the adapter's send_message is monkeypatched on the REAL module and
settings via a SimpleNamespace, so nothing touches a real channel.
"""

from __future__ import annotations

from types import SimpleNamespace

from app import deploy_notify as dn


def _stub_settings(monkeypatch, channel=""):
    monkeypatch.setattr(
        dn, "settings", SimpleNamespace(deploy_notify_discord_channel_id=channel)
    )


def _stub_adapter(monkeypatch, sink, raises=False):
    # Patch the REAL adapter module's send_message symbol. The production code
    # does ``from . import discord_adapter`` then ``discord_adapter.send_message``
    # -- patching the module attribute is robust even when the real adapter has
    # already been imported elsewhere in the suite (sys.modules swaps are not).
    import app.discord_adapter as real

    def _send(channel_id, text):
        if raises:
            raise RuntimeError("boom")
        sink.append((channel_id, text))
        return ["m1"]

    monkeypatch.setattr(real, "send_message", _send, raising=False)


def test_noop_without_channel(monkeypatch):
    _stub_settings(monkeypatch, "")
    sink: list = []
    _stub_adapter(monkeypatch, sink)
    assert dn.notify_deploy_complete_discord("abc1234567", 12.0) is False
    assert sink == []


def test_posts_to_configured_channel(monkeypatch):
    _stub_settings(monkeypatch, "999")
    sink: list = []
    _stub_adapter(monkeypatch, sink)
    assert dn.notify_deploy_complete_discord("abc1234567", 12.0) is True
    assert sink and str(sink[0][0]) == "999"
    assert "abc1234" in sink[0][1]
    assert "deploy complete" in sink[0][1].lower()


def test_explicit_channel_overrides_setting(monkeypatch):
    _stub_settings(monkeypatch, "")
    sink: list = []
    _stub_adapter(monkeypatch, sink)
    assert dn.notify_deploy_complete_discord("deadbeef", 3.0, channel_id=555) is True
    assert str(sink[0][0]) == "555"


def test_never_raises_on_adapter_error(monkeypatch):
    _stub_settings(monkeypatch, "999")
    _stub_adapter(monkeypatch, [], raises=True)
    assert dn.notify_deploy_complete_discord("abc1234", 1.0) is False


def test_unknown_commit_is_masked(monkeypatch):
    _stub_settings(monkeypatch, "999")
    sink: list = []
    _stub_adapter(monkeypatch, sink)
    dn.notify_deploy_complete_discord("unknown", 1.0)
    assert "unknown" in sink[0][1]
