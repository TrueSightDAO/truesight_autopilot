"""Unit tests for settings.handoffs_enabled and its effect on _handoff_prefix()."""

from __future__ import annotations

from app.config import Settings, settings


def test_handoffs_enabled_defaults_true():
    """Preserves Sophia's existing behavior."""
    assert Settings().handoffs_enabled is True


def test_handoffs_enabled_can_be_disabled():
    s = Settings(HANDOFFS_ENABLED="false")
    assert s.handoffs_enabled is False


def test_handoff_prefix_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "handoffs_enabled", False)
    from app.telegram_adapter import _handoff_prefix

    # A go-signal message on a thread_id that would normally trigger the
    # fallback framing regardless of registry match — must return "" instead.
    assert _handoff_prefix(16, "go for it") == ""
