"""Unit tests for the shared Google credential loader (path resolution only).

No network / no real keys — these exercise ``_candidate_paths`` resolution,
which is where the "wrong principal" bug lived.
"""

from __future__ import annotations

import json

from app.tools import google_creds as gc


def _write_key(path, email):
    path.write_text(json.dumps({"type": "service_account", "client_email": email}))


def test_unnamed_falls_back_to_env_default(tmp_path, monkeypatch):
    default = tmp_path / "cypher_defense_gdrive_key.json"
    _write_key(default, "cypher-defense@x")
    monkeypatch.setenv("GOOGLE_CREDS_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(default))
    assert gc.resolve_credentials_path(None) == str(default)


def test_named_resolves_its_own_file(tmp_path, monkeypatch):
    named = tmp_path / "agroverse_ledger_manager_gdrive_key.json"
    _write_key(named, "agroverse-ledger-manager@x")
    default = tmp_path / "cypher_defense_gdrive_key.json"
    _write_key(default, "cypher-defense@x")
    monkeypatch.setenv("GOOGLE_CREDS_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(default))
    assert gc.resolve_credentials_path("agroverse_ledger_manager") == str(named)


def test_named_hard_miss_never_falls_back_to_default(tmp_path, monkeypatch):
    """Regression guard: an unresolved name must NOT be served by the
    env-default SA — appending the default silently ran the call as the
    wrong principal (e.g. every ``agroverse_ledger_manager`` lookup)."""
    default = tmp_path / "cypher_defense_gdrive_key.json"
    _write_key(default, "cypher-defense@x")
    monkeypatch.setenv("GOOGLE_CREDS_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(default))
    assert gc.resolve_credentials_path("agroverse_ledger_manager") is None
    assert gc.resolve_credentials_path("totally_typo_name") is None


def test_named_also_tries_key_suffix(tmp_path, monkeypatch):
    named = tmp_path / "edgar_dapp_listener_key.json"
    _write_key(named, "edgar-dapp-listener@x")
    monkeypatch.setenv("GOOGLE_CREDS_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert gc.resolve_credentials_path("edgar_dapp_listener") == str(named)


def test_no_name_and_no_env_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CREDS_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert gc.resolve_credentials_path(None) is None
