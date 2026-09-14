"""Member-tier tests for the shared identity resolver (app/policy.py).

A verified contributor who is NOT a governor must resolve to Role.MEMBER
(not GUEST) -- the tier asked for on the Discord side, applied at the shared
policy layer so every front-end agrees.
"""

from __future__ import annotations

from app import policy


def _no_env_governors(monkeypatch):
    monkeypatch.setattr(policy, "_load_governor_telegram_ids", lambda: set())
    monkeypatch.setattr(policy, "_load_governor_names", lambda: set())


def test_env_allowlist_is_governor(monkeypatch):
    monkeypatch.setattr(policy, "_load_governor_telegram_ids", lambda: {123})
    monkeypatch.setattr(policy, "_load_governor_names", lambda: set())
    ident = policy.resolve_identity(telegram_id=123)
    assert ident.role == policy.Role.GOVERNOR


def test_bound_non_governor_is_member(monkeypatch):
    _no_env_governors(monkeypatch)
    monkeypatch.setattr(
        policy,
        "_resolve_binding",
        lambda tid: {"email": "theus.reis.ssa@gmail.com", "name": "Matheus Reis"},
    )
    monkeypatch.setattr(policy, "_binding_is_governor", lambda email, name: False)
    ident = policy.resolve_identity(telegram_id=42)
    assert ident.role == policy.Role.MEMBER
    assert ident.role != policy.Role.GOVERNOR
    assert ident.name == "Matheus Reis"


def test_bound_governor_is_governor(monkeypatch):
    _no_env_governors(monkeypatch)
    monkeypatch.setattr(
        policy,
        "_resolve_binding",
        lambda tid: {"email": "gary@truesight.me", "name": "Gary Teh"},
    )
    monkeypatch.setattr(policy, "_binding_is_governor", lambda email, name: True)
    ident = policy.resolve_identity(telegram_id=42)
    assert ident.role == policy.Role.GOVERNOR


def test_unbound_is_guest(monkeypatch):
    _no_env_governors(monkeypatch)
    monkeypatch.setattr(policy, "_resolve_binding", lambda tid: None)
    ident = policy.resolve_identity(telegram_id=42)
    assert ident.role == policy.Role.GUEST
