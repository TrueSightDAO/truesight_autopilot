"""Unit test for settings.followups_enabled default (§5d — zero behavior change)."""

from __future__ import annotations

from app.config import Settings


def test_followups_enabled_defaults_true():
    """Preserves Sophia's existing behavior — the follow-up loop always started."""
    assert Settings().followups_enabled is True


def test_followups_enabled_can_be_disabled():
    s = Settings(FOLLOWUPS_ENABLED="false")
    assert s.followups_enabled is False
