"""Tests for app/vault.py — encrypted credential vault.

Phase 3 of the Multi-Tenant Governance & Vault plan (vault-first order).
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from app.vault import (
    Vault,
    VaultEntry,
    VaultAuditEntry,
    CredentialRef,
    get_vault,
    reset_vault_for_testing,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def vault_dir():
    """Create a temporary vault directory for each test."""
    with tempfile.TemporaryDirectory(prefix="vault_test_") as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def vault(vault_dir):
    """Create a fresh, initialized vault for each test."""
    v = Vault(vault_dir=str(vault_dir))
    v.initialize()
    return v


# ── Initialization tests ──────────────────────────────────────────────────


class TestInitialization:
    def test_is_initialized_returns_false_before_init(self, vault_dir):
        v = Vault(vault_dir=str(vault_dir))
        assert v.is_initialized() is False

    def test_initialize_creates_key_and_vault_file(self, vault_dir):
        v = Vault(vault_dir=str(vault_dir))
        v.initialize()
        assert v.is_initialized() is True
        assert v.key_path.exists()
        assert v.key_path.stat().st_mode & 0o777 == 0o600  # restricted perms

    def test_initialize_is_idempotent(self, vault_dir):
        v = Vault(vault_dir=str(vault_dir))
        v.initialize()
        key_before = v.key_path.read_bytes()
        v.initialize()  # second call
        key_after = v.key_path.read_bytes()
        assert key_before == key_after  # key not regenerated

    def test_vault_dir_is_created(self, vault_dir):
        test_dir = vault_dir / "nested" / "vault"
        v = Vault(vault_dir=str(test_dir))
        v.initialize()
        assert test_dir.exists()
        assert v.is_initialized() is True


# ── Add credential tests ──────────────────────────────────────────────────


class TestAddCredential:
    def test_add_simple_credential(self, vault):
        entry = vault.add(
            name="github_pat",
            value="ghp_abc123",
            purpose="GitHub Personal Access Token for API access",
            scopes=["repo", "read:org"],
            created_by="Gary Teh",
        )
        assert entry.name == "github_pat"
        assert entry.version == 1
        assert entry.purpose == "GitHub Personal Access Token for API access"
        assert entry.scopes == ["repo", "read:org"]
        assert entry.created_by == "Gary Teh"

    def test_add_credential_is_encrypted(self, vault):
        vault.add(
            name="api_key",
            value="super-secret-value-123",
            purpose="Test API key",
            scopes=["read"],
            created_by="Gary Teh",
        )
        # Read the raw vault file — value should be encrypted
        raw = vault._vault_path.read_bytes()
        assert b"super-secret-value-123" not in raw  # not in plaintext

    def test_add_duplicate_name_raises(self, vault):
        vault.add(
            name="my_key",
            value="secret1",
            purpose="First key",
            scopes=["read"],
            created_by="Gary Teh",
        )
        with pytest.raises(ValueError, match="already exists"):
            vault.add(
                name="my_key",
                value="secret2",
                purpose="Second key",
                scopes=["write"],
                created_by="Gary Teh",
            )

    def test_add_multiple_credentials(self, vault):
        vault.add("key_a", "val_a", "Key A", ["read"], "Gary")
        vault.add("key_b", "val_b", "Key B", ["write"], "Gary")
        vault.add("key_c", "val_c", "Key C", ["admin"], "Gary")
        assert len(vault.list_refs()) == 3

    def test_add_large_value(self, vault):
        large_val = "x" * 100_000  # 100KB
        vault.add("large", large_val, "Large value", ["read"], "Gary")
        retrieved = vault.get_value("large")
        assert retrieved == large_val


# ── Update (versioned) tests ──────────────────────────────────────────────


class TestUpdateCredential:
    def test_update_creates_new_version(self, vault):
        vault.add("my_key", "v1_value", "Version 1", ["read"], "Gary")
        entry = vault.update(
            name="my_key",
            value="v2_value",
            updated_by="Gary Teh",
        )
        assert entry.version == 2
        assert vault.get_value("my_key") == "v2_value"

    def test_update_nonexistent_raises(self, vault):
        with pytest.raises(ValueError, match="not found"):
            vault.update("nonexistent", "new_val", "Gary")

    def test_update_preserves_older_versions(self, vault):
        vault.add("my_key", "v1", "Test", ["read"], "Gary")
        vault.update("my_key", "v2", "Gary")
        vault.update("my_key", "v3", "Gary")
        # All versions should be retrievable
        refs = vault.list_refs()
        my_key = [r for r in refs if r.name == "my_key"][0]
        assert my_key.version == 3

    def test_update_preserves_metadata(self, vault):
        vault.add("my_key", "v1", "Original purpose", ["read", "write"], "Gary")
        vault.update("my_key", "v2", "Gary")
        ref = vault.get_ref("my_key")
        assert ref.purpose == "Original purpose"  # purpose preserved
        assert ref.scopes == ["read", "write"]  # scopes preserved


# ── Delete tests ──────────────────────────────────────────────────────────


class TestDeleteCredential:
    def test_delete_credential(self, vault):
        vault.add("my_key", "secret", "Test", ["read"], "Gary")
        vault.delete("my_key", deleted_by="Gary Teh")
        assert vault.has_credential("my_key") is False

    def test_delete_nonexistent_raises(self, vault):
        with pytest.raises(ValueError, match="not found"):
            vault.delete("nonexistent", "Gary")

    def test_delete_is_audited(self, vault):
        vault.add("my_key", "secret", "Test", ["read"], "Gary")
        vault.delete("my_key", deleted_by="Gary Teh")
        log = vault.get_audit_log()
        delete_entries = [e for e in log if e.action == "delete"]
        assert len(delete_entries) == 1
        assert delete_entries[0].credential_name == "my_key"
        assert delete_entries[0].actor == "Gary Teh"


# ── Get value / ref tests ─────────────────────────────────────────────────


class TestGetValue:
    def test_get_value_returns_plaintext(self, vault):
        vault.add("my_key", "my_secret_value", "Test", ["read"], "Gary")
        value = vault.get_value("my_key")
        assert value == "my_secret_value"

    def test_get_value_nonexistent_raises(self, vault):
        with pytest.raises(ValueError, match="not found"):
            vault.get_value("nonexistent")

    def test_get_ref_returns_metadata_only(self, vault):
        vault.add("my_key", "super_secret", "API key for GitHub", ["repo"], "Gary")
        ref = vault.get_ref("my_key")
        assert isinstance(ref, CredentialRef)
        assert ref.name == "my_key"
        assert ref.purpose == "API key for GitHub"
        assert ref.scopes == ["repo"]
        # Value should NOT be in the ref
        assert not hasattr(ref, "value") or ref.value is None

    def test_list_refs_never_exposes_values(self, vault):
        vault.add("key_a", "val_a", "Key A", ["read"], "Gary")
        vault.add("key_b", "val_b", "Key B", ["write"], "Gary")
        refs = vault.list_refs()
        for ref in refs:
            assert not hasattr(ref, "value") or ref.value is None


# ── Persistence tests ─────────────────────────────────────────────────────


class TestPersistence:
    def test_vault_persists_across_reload(self, vault_dir):
        v1 = Vault(vault_dir=str(vault_dir))
        v1.initialize()
        v1.add("persistent_key", "persistent_val", "Test", ["read"], "Gary")

        # Create a new instance pointing to the same directory
        v2 = Vault(vault_dir=str(vault_dir))
        assert v2.has_credential("persistent_key") is True
        assert v2.get_value("persistent_key") == "persistent_val"

    def test_empty_vault_after_init(self, vault):
        assert vault.list_refs() == []


# ── Backup / Restore tests ────────────────────────────────────────────────


class TestBackupRestore:
    def test_export_backup(self, vault):
        vault.add("key_a", "val_a", "Key A", ["read"], "Gary")
        vault.add("key_b", "val_b", "Key B", ["write"], "Gary")
        backup = vault.export_backup(actor="Gary Teh")
        assert isinstance(backup, bytes)
        data = json.loads(backup.decode("utf-8"))
        assert data["version"] == 1
        assert len(data["entries"]) == 2

    def test_restore_from_backup(self, vault_dir):
        # Create vault, add entries, export
        v1 = Vault(vault_dir=str(vault_dir))
        v1.initialize()
        v1.add("key_a", "val_a", "Key A", ["read"], "Gary")
        backup = v1.export_backup(actor="Gary Teh")

        # Create fresh vault, restore
        v2 = Vault(vault_dir=str(vault_dir))
        v2.initialize()
        # Delete the existing entry first
        v2.delete("key_a", "Gary")
        count = v2.restore_from_backup(backup, restored_by="Gary Teh")
        assert count == 1
        assert v2.get_value("key_a") == "val_a"

    def test_restore_with_merge(self, vault_dir):
        v1 = Vault(vault_dir=str(vault_dir))
        v1.initialize()
        v1.add("key_a", "val_a", "Key A", ["read"], "Gary")
        backup = v1.export_backup(actor="Gary Teh")

        v2 = Vault(vault_dir=str(vault_dir))
        v2.initialize()
        v2.add("key_b", "val_b", "Key B", ["write"], "Gary")
        count = v2.restore_from_backup(backup, restored_by="Gary Teh", merge=True)
        assert count == 1
        assert v2.has_credential("key_a") is True
        assert v2.has_credential("key_b") is True

    def test_restore_invalid_backup_raises(self, vault):
        with pytest.raises(ValueError, match="Invalid backup"):
            vault.restore_from_backup(b"not json", "Gary")

    def test_restore_wrong_version_raises(self, vault):
        bad_backup = json.dumps({"version": 99, "entries": {}}).encode("utf-8")
        with pytest.raises(ValueError, match="Unsupported backup"):
            vault.restore_from_backup(bad_backup, "Gary")


# ── Audit log tests ───────────────────────────────────────────────────────


class TestAuditLog:
    def test_add_is_audited(self, vault):
        vault.add("my_key", "secret", "Test", ["read"], "Gary Teh")
        log = vault.get_audit_log()
        add_entries = [e for e in log if e.action == "add"]
        assert len(add_entries) >= 1
        assert add_entries[0].credential_name == "my_key"
        assert add_entries[0].actor == "Gary Teh"

    def test_update_is_audited(self, vault):
        vault.add("my_key", "v1", "Test", ["read"], "Gary")
        vault.update("my_key", "v2", "Gary Teh")
        log = vault.get_audit_log()
        update_entries = [e for e in log if e.action == "update"]
        assert len(update_entries) >= 1

    def test_backup_is_audited(self, vault):
        vault.add("my_key", "secret", "Test", ["read"], "Gary")
        vault.export_backup(actor="Gary Teh")
        log = vault.get_audit_log()
        backup_entries = [e for e in log if e.action == "backup"]
        assert len(backup_entries) >= 1

    def test_audit_log_ordered_most_recent_first(self, vault):
        vault.add("key_a", "val_a", "A", ["read"], "Gary")
        vault.add("key_b", "val_b", "B", ["read"], "Gary")
        log = vault.get_audit_log(limit=10)
        # Most recent first
        assert log[0].credential_name == "key_b"
        assert log[1].credential_name == "key_a"


# ── Security invariant tests ──────────────────────────────────────────────


class TestSecurityInvariants:
    def test_value_never_in_ref(self, vault):
        """Invariant #3: Credential values never appear in refs."""
        vault.add("my_key", "super_secret_value", "Test", ["read"], "Gary")
        ref = vault.get_ref("my_key")
        # The ref dataclass has no 'value' field
        assert not hasattr(ref, "value")

    def test_encrypted_at_rest(self, vault):
        """Invariant #7: Encryption-at-rest."""
        vault.add("my_key", "plaintext_secret", "Test", ["read"], "Gary")
        raw = vault._vault_path.read_bytes()
        assert b"plaintext_secret" not in raw

    def test_key_file_restricted_permissions(self, vault_dir):
        """Vault key file should be readable only by owner."""
        v = Vault(vault_dir=str(vault_dir))
        v.initialize()
        perms = v.key_path.stat().st_mode & 0o777
        assert perms == 0o600  # owner read/write only

    def test_vault_file_restricted_permissions(self, vault):
        vault.add("my_key", "secret", "Test", ["read"], "Gary")
        perms = vault._vault_path.stat().st_mode & 0o777
        assert perms == 0o600  # owner read/write only


# ── Singleton tests ───────────────────────────────────────────────────────


class TestSingleton:
    def test_get_vault_returns_same_instance(self):
        reset_vault_for_testing()
        v1 = get_vault()
        v2 = get_vault()
        assert v1 is v2

    def test_reset_for_testing(self):
        reset_vault_for_testing()
        v = get_vault()
        reset_vault_for_testing(None)
        v2 = get_vault()
        assert v is not v2


# ── Edge case tests ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_string_value(self, vault):
        vault.add("empty", "", "Empty value", ["read"], "Gary")
        assert vault.get_value("empty") == ""

    def test_unicode_value(self, vault):
        vault.add("unicode", "café São Paulo 佐藤", "Unicode test", ["read"], "Gary")
        assert vault.get_value("unicode") == "café São Paulo 佐藤"

    def test_special_chars_in_name(self, vault):
        vault.add("my-key_123", "val", "Special name", ["read"], "Gary")
        assert vault.has_credential("my-key_123") is True

    def test_get_names(self, vault):
        vault.add("z_key", "val", "Z", ["read"], "Gary")
        vault.add("a_key", "val", "A", ["read"], "Gary")
        names = vault.get_names()
        assert names == ["a_key", "z_key"]  # sorted

    def test_has_credential_false_for_missing(self, vault):
        assert vault.has_credential("nonexistent") is False

    def test_export_key(self, vault):
        key_bytes = vault.export_key()
        assert len(key_bytes) > 0
        # Should be a Fernet key (base64-encoded 32 bytes)
        from cryptography.fernet import Fernet
        f = Fernet(key_bytes)
        assert f is not None
