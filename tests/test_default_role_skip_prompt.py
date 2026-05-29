"""Tests for AUTOPILOT_DEFAULT_ROLE — operator opt-out for the
new-topic role-selection prompt."""
from __future__ import annotations

from app.roles import ROLES, get_default_role


def test_default_general_out_of_box(monkeypatch):
    # No env override at all → defaults to "general" per Gary's 2026-05-29 ask.
    monkeypatch.delenv("AUTOPILOT_DEFAULT_ROLE", raising=False)
    role = get_default_role()
    assert role is not None
    assert role.key == "general"


def test_explicit_default_role(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DEFAULT_ROLE", "infrastructure")
    role = get_default_role()
    assert role is not None
    assert role.key == "infrastructure"


def test_empty_env_restores_prompt(monkeypatch):
    # Explicit empty → no default → telegram adapter falls through to the menu.
    monkeypatch.setenv("AUTOPILOT_DEFAULT_ROLE", "")
    assert get_default_role() is None


def test_whitespace_normalisation(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DEFAULT_ROLE", "  Infrastructure  ")
    role = get_default_role()
    assert role is not None
    assert role.key == "infrastructure"


def test_alias_lookup(monkeypatch):
    # "sre" is an alias for "infrastructure" per ROLE_ALIASES.
    monkeypatch.setenv("AUTOPILOT_DEFAULT_ROLE", "sre")
    role = get_default_role()
    assert role is not None
    assert role.key == "infrastructure"


def test_unknown_value(monkeypatch):
    # Unknown role name → None. Telegram adapter falls back to the prompt.
    monkeypatch.setenv("AUTOPILOT_DEFAULT_ROLE", "not_a_real_role")
    assert get_default_role() is None
