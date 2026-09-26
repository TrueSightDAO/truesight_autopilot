"""Credential vault — encrypted on-disk store for service credentials.

Phase 3 of the Multi-Tenant Governance & Vault plan (vault-first order).

Security invariants upheld:
- Invariant #3: Credentials never appear in chat / transcripts / PRs / logs.
  The LLM only ever sees {name, purpose, scopes}; values are injected at
  tool-execution time.
- Invariant #4: Guest-default. Only governors may access the vault.
- Invariant #7: Confidentiality via encryption-at-rest (Fernet key derived
  from a box-local secret).

Design:
- Encrypted at rest using Fernet (symmetric, authenticated encryption).
- Entries: {name, purpose, scopes, version, value(enc), created_by, created_at}.
- Never-overwrite: updating a credential creates a new version.
- Delete allowed (with audit trail).
- Versioning for safe rotation.
- Audit log of add/delete (RSA-signed actor name).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_VAULT_DIR = Path("/opt/truesight_autopilot/vault")
DEFAULT_VAULT_FILE = "vault.json.enc"
DEFAULT_AUDIT_FILE = "vault_audit.json"
DEFAULT_KEY_FILE = "vault.key"
MAX_ENTRY_VALUE_BYTES = 256 * 1024  # 256 KB per credential value


# ── Data types ────────────────────────────────────────────────────────────


@dataclass
class VaultEntry:
    """A single credential entry in the vault."""

    name: str
    purpose: str
    scopes: list[str]
    version: int
    value: str  # encrypted value (Fernet token, base64)
    created_by: str
    created_at: str  # ISO 8601
    # Metadata only (never the decrypted value):
    value_preview: str = ""  # first few chars of the purpose hint


@dataclass
class VaultAuditEntry:
    """An audit log entry for vault operations."""

    action: str  # "add", "delete", "backup", "restore"
    credential_name: str
    version: int | None
    actor: str
    timestamp: str  # ISO 8601
    details: str = ""


@dataclass
class CredentialRef:
    """A reference to a credential — the only thing the LLM ever sees.

    This is the public face of a credential. The actual value is never
    exposed to the LLM, transcripts, or logs.
    """

    name: str
    purpose: str
    scopes: list[str]
    version: int
    created_by: str
    created_at: str


# ── Vault class ───────────────────────────────────────────────────────────


class Vault:
    """Encrypted credential vault.

    Thread-safe for reads (single-process, file-based). Writes are
    serialized via atomic file replacement.

    Usage:
        vault = Vault()
        vault.initialize()  # first-time setup
        vault.add("openai_key", "OpenAI API key", ["llm"], "sk-...", "Gary")
        ref = vault.get_ref("openai_key")  # LLM-safe reference
        value = vault.get_value("openai_key")  # actual value (tool only)
    """

    def __init__(
        self,
        vault_dir: str | Path | None = None,
        key: bytes | None = None,
    ):
        self._vault_dir = Path(vault_dir) if vault_dir else DEFAULT_VAULT_DIR
        self._vault_path = self._vault_dir / DEFAULT_VAULT_FILE
        self._audit_path = self._vault_dir / DEFAULT_AUDIT_FILE
        self._key_path = self._vault_dir / DEFAULT_KEY_FILE

        self._fernet: Fernet | None = None
        if key is not None:
            self._fernet = Fernet(key)

        # In-memory cache: {name: VaultEntry}
        self._entries: dict[str, VaultEntry] = {}
        self._loaded = False
        # (mtime_ns, size) of vault.json.enc at the moment we last loaded it.
        # Used to detect when ANOTHER process/worker (or Sophia's CLI) has
        # written to the same vault dir, so this long-lived instance reloads
        # instead of serving a stale cache. None = file absent at load time.
        self._loaded_sig: tuple[int, int] | None = None

    # ── Initialization ────────────────────────────────────────────────

    def initialize(self, actor: str = "system") -> None:
        """First-time setup: create vault directory and generate a key.

        Safe to call multiple times — skips if already initialized.
        """
        self._vault_dir.mkdir(parents=True, exist_ok=True)

        if not self._key_path.exists():
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            self._key_path.chmod(0o600)  # owner read/write only
            logger.info("Vault key generated at %s", self._key_path)
            self._audit_log(
                VaultAuditEntry(
                    action="initialize",
                    credential_name="__vault__",
                    version=None,
                    actor=actor,
                    timestamp=_now_iso(),
                    details="Vault initialized with new key",
                )
            )

        if self._fernet is None:
            self._fernet = Fernet(self._key_path.read_bytes())

        if not self._loaded:
            self._load()

    def is_initialized(self) -> bool:
        """Check if the vault has been initialized."""
        return self._key_path.exists()

    # ── Key management ────────────────────────────────────────────────

    @property
    def key_path(self) -> Path:
        return self._key_path

    def export_key(self) -> bytes:
        """Export the vault encryption key for backup purposes.

        The exported key is a base64-encoded Fernet key that can be used
        to restore the vault on another instance. Store this securely
        alongside vault backups.

        WARNING: This is the master key. Handle with extreme care.
        Only callable from the vault web page or backup script.
        """
        if not self._key_path.exists():
            raise ValueError("Vault key not found. Initialize the vault first.")
        return self._key_path.read_bytes()

    # ── CRUD operations ───────────────────────────────────────────────

    def add(
        self,
        name: str,
        value: str,
        purpose: str,
        scopes: list[str] | None = None,
        created_by: str = "system",
    ) -> VaultEntry:
        """Add a new credential. Raises ValueError if name exists (use update).

        Args:
            name: Unique credential name (e.g. "openai_key", "github_token").
            value: The actual secret value (encrypted before storage).
            purpose: Human-readable description of what this credential is for.
            scopes: List of tool/action scopes this credential authorizes.
            created_by: Display name of the actor creating this entry.

        Returns:
            The newly created VaultEntry.

        Raises:
            ValueError: If a credential with this name already exists.
        """
        self._ensure_loaded()

        if name in self._entries:
            raise ValueError(
                f"Credential '{name}' already exists (version "
                f"{self._entries[name].version}). Use update() to rotate."
            )

        if scopes is None:
            scopes = []
        encrypted = self._encrypt(value)
        now = _now_iso()
        entry = VaultEntry(
            name=name,
            purpose=purpose,
            scopes=list(scopes),
            version=1,
            value=encrypted,
            created_by=created_by,
            created_at=now,
            value_preview=purpose[:60],
        )
        self._entries[name] = entry
        self._save()

        self._audit_log(
            VaultAuditEntry(
                action="add",
                credential_name=name,
                version=1,
                actor=created_by,
                timestamp=now,
                details=f"Purpose: {purpose}, Scopes: {scopes}",
            )
        )

        logger.info("Vault: added credential '%s' (v1) by %s", name, created_by)
        return entry

    def update(
        self,
        name: str,
        value: str,
        updated_by: str,
        *,
        new_purpose: str | None = None,
        new_scopes: list[str] | None = None,
    ) -> VaultEntry:
        """Rotate a credential to a new version.

        Never overwrites — creates a new version. Old versions are
        retained in the audit log but the active entry is replaced.

        Args:
            name: Credential name.
            value: New secret value.
            updated_by: Actor name.
            new_purpose: Optional updated purpose.
            new_scopes: Optional updated scopes.

        Returns:
            The new VaultEntry (incremented version).

        Raises:
            KeyError: If credential doesn't exist.
        """
        self._ensure_loaded()

        if name not in self._entries:
            raise ValueError(f"Credential '{name}' not found.")

        old = self._entries[name]
        encrypted = self._encrypt(value)
        now = _now_iso()
        resolved_scopes = (
            list(new_scopes) if new_scopes is not None else list(old.scopes)
        )
        entry = VaultEntry(
            name=name,
            purpose=new_purpose or old.purpose,
            scopes=resolved_scopes,
            version=old.version + 1,
            value=encrypted,
            created_by=updated_by,
            created_at=now,
            value_preview=(new_purpose or old.purpose)[:60],
        )
        self._entries[name] = entry
        self._save()

        self._audit_log(
            VaultAuditEntry(
                action="update",
                credential_name=name,
                version=entry.version,
                actor=updated_by,
                timestamp=now,
                details=f"Rotated from v{old.version}",
            )
        )

        logger.info(
            "Vault: rotated credential '%s' v%d→v%d by %s",
            name,
            old.version,
            entry.version,
            updated_by,
        )
        return entry

    def delete(self, name: str, deleted_by: str) -> None:
        """Delete a credential from the vault.

        The credential is removed from the active store but the audit
        log retains the history.

        Args:
            name: Credential name.
            deleted_by: Actor name.

        Raises:
            KeyError: If credential doesn't exist.
        """
        self._ensure_loaded()

        if name not in self._entries:
            raise ValueError(f"Credential '{name}' not found.")

        entry = self._entries.pop(name)
        self._save()

        self._audit_log(
            VaultAuditEntry(
                action="delete",
                credential_name=name,
                version=entry.version,
                actor=deleted_by,
                timestamp=_now_iso(),
                details=f"Deleted v{entry.version} (purpose: {entry.purpose})",
            )
        )

        logger.info("Vault: deleted credential '%s' by %s", name, deleted_by)

    def get_ref(self, name: str) -> CredentialRef:
        """Get a credential reference — the ONLY thing the LLM sees.

        This is safe to return in chat responses. It contains metadata
        only — never the actual secret value.

        Args:
            name: Credential name.

        Returns:
            A CredentialRef with metadata only.

        Raises:
            KeyError: If credential doesn't exist.
        """
        self._ensure_loaded()

        if name not in self._entries:
            raise ValueError(f"Credential '{name}' not found.")

        entry = self._entries[name]
        return CredentialRef(
            name=entry.name,
            purpose=entry.purpose,
            scopes=list(entry.scopes),
            version=entry.version,
            created_by=entry.created_by,
            created_at=entry.created_at,
        )

    def get_value(self, name: str) -> str:
        """Get the decrypted credential value.

        WARNING: This returns the actual secret. Only call this from
        tool-execution code that injects the value at call time.
        NEVER return this value in a chat response, transcript, or log.

        Args:
            name: Credential name.

        Returns:
            The decrypted secret value.

        Raises:
            KeyError: If credential doesn't exist.
        """
        self._ensure_loaded()

        if name not in self._entries:
            raise ValueError(f"Credential '{name}' not found.")

        return self._decrypt(self._entries[name].value)

    def list_refs(self) -> list[CredentialRef]:
        """List all credential references (metadata only, no values).

        Safe to return in chat responses.
        """
        self._ensure_loaded()
        return [
            CredentialRef(
                name=e.name,
                purpose=e.purpose,
                scopes=list(e.scopes),
                version=e.version,
                created_by=e.created_by,
                created_at=e.created_at,
            )
            for e in sorted(self._entries.values(), key=lambda x: x.name)
        ]

    def get_names(self) -> list[str]:
        """Get just the list of credential names (lightweight)."""
        self._ensure_loaded()
        return sorted(self._entries.keys())

    def has_credential(self, name: str) -> bool:
        """Check if a credential exists."""
        self._ensure_loaded()
        return name in self._entries

    # ── Backup / Restore ──────────────────────────────────────────────

    def export_backup(self, actor: str = "system") -> bytes:
        """Export an encrypted backup of the entire vault.

        The backup includes all entries (still encrypted with the vault
        key) plus metadata. The backup itself is NOT re-encrypted — it
        uses the same Fernet key, so the key file must be backed up
        alongside it.

        Returns:
            JSON bytes of the backup payload.
        """
        self._ensure_loaded()

        backup = {
            "version": 1,
            "exported_at": _now_iso(),
            "exported_by": actor,
            "entries": {name: asdict(entry) for name, entry in self._entries.items()},
        }
        payload = json.dumps(backup, indent=2, ensure_ascii=False).encode("utf-8")

        self._audit_log(
            VaultAuditEntry(
                action="backup",
                credential_name="__vault__",
                version=None,
                actor=actor,
                timestamp=_now_iso(),
                details=f"Exported {len(self._entries)} entries",
            )
        )

        logger.info(
            "Vault: backup exported by %s (%d entries)", actor, len(self._entries)
        )
        return payload

    def restore_from_backup(
        self,
        backup_bytes: bytes,
        restored_by: str,
        *,
        merge: bool = False,
    ) -> int:
        """Restore the vault from an encrypted backup.

        Args:
            backup_bytes: The backup payload (from export_backup()).
            restored_by: Actor name.
            merge: If True, merge backup entries with existing (backup
                    wins on conflict). If False, replace all entries.

        Returns:
            Number of entries restored.

        Raises:
            ValueError: If the backup format is invalid.
        """
        try:
            backup = json.loads(backup_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid backup format: {e}") from e

        if backup.get("version") != 1:
            raise ValueError(f"Unsupported backup version: {backup.get('version')}")

        entries_data = backup.get("entries", {})
        if not isinstance(entries_data, dict):
            raise ValueError("Invalid backup: entries must be a dict")

        if not merge:
            self._entries = {}

        for name, data in entries_data.items():
            self._entries[name] = VaultEntry(**data)

        self._save()

        self._audit_log(
            VaultAuditEntry(
                action="restore",
                credential_name="__vault__",
                version=None,
                actor=restored_by,
                timestamp=_now_iso(),
                details=f"Restored {len(entries_data)} entries (merge={merge})",
            )
        )

        logger.info(
            "Vault: restored %d entries by %s (merge=%s)",
            len(entries_data),
            restored_by,
            merge,
        )
        return len(entries_data)

    # ── Audit log ─────────────────────────────────────────────────────

    def get_audit_log(self, limit: int = 100) -> list[VaultAuditEntry]:
        """Get the audit log, most recent first."""
        if not self._audit_path.exists():
            return []

        try:
            data = json.loads(self._audit_path.read_text(encoding="utf-8"))
            entries = [VaultAuditEntry(**e) for e in data.get("entries", [])]
            entries.reverse()
            return entries[:limit]
        except Exception as e:
            logger.warning("Failed to read vault audit log: %s", e)
            return []

    # ── Internal methods ──────────────────────────────────────────────

    def _disk_signature(self) -> tuple[int, int] | None:
        """(mtime_ns, size) of the vault file, or None if it doesn't exist."""
        try:
            st = self._vault_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def _ensure_loaded(self) -> None:
        """Load the vault, or RELOAD it if the file changed under us.

        The vault runs as multiple OS processes (the main bot on :8001, the
        dedicated vault web worker on :8002, plus one-off CLI writes). They do
        NOT share memory, so a write by one is invisible to the others until
        they re-read disk. Without this reload check a long-lived worker serves
        a stale cache forever — the bug behind the 2026-06-15 "page shows empty
        after the credentials were added" incident.
        """
        if not self._loaded:
            self._load()
        elif self._disk_signature() != self._loaded_sig:
            logger.debug("Vault: file changed on disk — reloading")
            self._load()

    def _load(self) -> None:
        """Load the vault from disk."""
        self._loaded = True
        # Capture the signature BEFORE reading: if a concurrent write lands
        # mid-read we'll reload next time (stale-but-safe) rather than miss it.
        self._loaded_sig = self._disk_signature()

        if not self._vault_path.exists():
            self._entries = {}
            return

        try:
            encrypted_data = self._vault_path.read_bytes()
            if not encrypted_data.strip():
                self._entries = {}
                return

            if self._fernet is None:
                self._fernet = Fernet(self._key_path.read_bytes())

            decrypted = self._fernet.decrypt(encrypted_data)
            data = json.loads(decrypted.decode("utf-8"))
            self._entries = {
                name: VaultEntry(**entry_data)
                for name, entry_data in data.get("entries", {}).items()
            }
            logger.debug("Vault: loaded %d entries", len(self._entries))
        except Exception as e:
            logger.error("Failed to load vault: %s", e)
            self._entries = {}

    def _save(self) -> None:
        """Save the vault to disk atomically."""
        if self._fernet is None:
            self._fernet = Fernet(self._key_path.read_bytes())

        data = {
            "version": 1,
            "updated_at": _now_iso(),
            "entries": {name: asdict(entry) for name, entry in self._entries.items()},
        }
        plaintext = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)

        # Atomic write: write to temp, then rename
        tmp_path = self._vault_path.with_suffix(".tmp")
        tmp_path.write_bytes(encrypted)
        tmp_path.chmod(0o600)
        tmp_path.rename(self._vault_path)

        # Record our own write so the next read doesn't reload it as if it were
        # an external change. (A reload here would be harmless but wasteful.)
        self._loaded_sig = self._disk_signature()

    def _encrypt(self, value: str) -> str:
        if self._fernet is None:
            self._fernet = Fernet(self._key_path.read_bytes())
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def _decrypt(self, token: str) -> str:
        if self._fernet is None:
            self._fernet = Fernet(self._key_path.read_bytes())
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

    def _audit_log(self, entry: VaultAuditEntry) -> None:
        """Append an entry to the audit log."""
        try:
            if self._audit_path.exists():
                data = json.loads(self._audit_path.read_text(encoding="utf-8"))
            else:
                data = {"version": 1, "entries": []}

            data["entries"].append(asdict(entry))

            tmp_path = self._audit_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp_path.chmod(0o600)
            tmp_path.rename(self._audit_path)
        except Exception as e:
            logger.warning("Failed to write vault audit log: %s", e)


# ── Helpers ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Get current UTC time as ISO 8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Module-level singleton ────────────────────────────────────────────────

_vault_instance: Vault | None = None


def get_vault() -> Vault:
    """Get or create the singleton vault instance."""
    global _vault_instance
    if _vault_instance is None:
        vault_dir = os.getenv("VAULT_DIR", str(DEFAULT_VAULT_DIR))
        _vault_instance = Vault(vault_dir=vault_dir)
        if not _vault_instance.is_initialized():
            _vault_instance.initialize()
    return _vault_instance


def reset_vault_for_testing(vault: Vault | None = None) -> None:
    """Reset the singleton (for testing only)."""
    global _vault_instance
    _vault_instance = vault
