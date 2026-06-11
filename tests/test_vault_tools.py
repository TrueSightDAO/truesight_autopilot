"""Tests for app/tools/vault_tools.py — vault interaction tools (Phase 3.5, 3.6)."""

import tempfile
from pathlib import Path

import pytest

from app.vault import Vault, reset_vault_for_testing
from app.tools.vault_tools import (
    check_credential,
    get_vault_url,
    report_missing_credential,
)


@pytest.fixture(autouse=True)
def _fresh_vault():
    """Create a fresh vault for each test."""
    with tempfile.TemporaryDirectory(prefix="vault_test_") as tmpdir:
        v = Vault(vault_dir=str(tmpdir))
        v.initialize()
        reset_vault_for_testing(v)
        yield
        reset_vault_for_testing(None)


class TestGetVaultUrl:
    def test_returns_url_string(self):
        url = get_vault_url()
        assert isinstance(url, str)
        assert len(url) > 0
        assert url.startswith("/")


class TestCheckCredential:
    def test_credential_found_returns_metadata(self):
        vault = Vault(vault_dir=tempfile.mkdtemp())
        vault.initialize()
        vault.add("test_key", "secret_value", "Testing", ["read"], "Gary")
        reset_vault_for_testing(vault)

        result = check_credential("test_key")
        assert result["found"] is True
        assert result["name"] == "test_key"
        assert result["purpose"] == "Testing"
        assert result["scopes"] == ["read"]
        assert result["version"] == 1
        assert result["created_by"] == "Gary"
        # Value must NEVER be in the result
        assert "value" not in result
        assert "secret_value" not in str(result)

    def test_credential_not_found(self):
        vault = Vault(vault_dir=tempfile.mkdtemp())
        vault.initialize()
        reset_vault_for_testing(vault)

        result = check_credential("nonexistent")
        assert result["found"] is False
        assert "not found" in result.get("error", "")
        assert "vault_url" in result

    def test_vault_not_initialized(self):
        with tempfile.TemporaryDirectory(prefix="vault_test_") as tmpdir:
            v = Vault(vault_dir=str(tmpdir))
            # Don't initialize
            reset_vault_for_testing(v)

            result = check_credential("anything")
            assert result["found"] is False
            assert "not initialized" in result.get("error", "").lower()

    def test_vault_url_included(self):
        vault = Vault(vault_dir=tempfile.mkdtemp())
        vault.initialize()
        vault.add("k", "v", "p", [], "Gary")
        reset_vault_for_testing(vault)

        result = check_credential("k")
        assert "vault_url" in result
        assert isinstance(result["vault_url"], str)


class TestReportMissingCredential:
    def test_reports_missing(self):
        vault = Vault(vault_dir=tempfile.mkdtemp())
        vault.initialize()
        reset_vault_for_testing(vault)

        msg = report_missing_credential("stripe_key", "Stripe payments")
        assert "stripe_key" in msg
        assert "Stripe payments" in msg
        assert "/vault" in msg

    def test_reports_when_credential_exists(self):
        vault = Vault(vault_dir=tempfile.mkdtemp())
        vault.initialize()
        vault.add("stripe_key", "sk_test_xxx", "Stripe payments", ["read"], "Gary")
        reset_vault_for_testing(vault)

        msg = report_missing_credential("stripe_key", "Stripe payments")
        assert "exists" in msg.lower()
        assert "/vault" in msg

    def test_reports_when_vault_not_initialized(self):
        with tempfile.TemporaryDirectory(prefix="vault_test_") as tmpdir:
            v = Vault(vault_dir=str(tmpdir))
            reset_vault_for_testing(v)

            msg = report_missing_credential("any_key", "any purpose")
            assert "not been initialized" in msg
            assert "/vault" in msg
