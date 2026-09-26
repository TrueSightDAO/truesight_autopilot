"""Sophia tools for the credential vault — Phase 3.5 and 3.6.

These tools let Sophia interact with the vault in chat:
- get_vault_url: returns the (non-secret) vault URL
- check_credential: checks if a credential exists, returns metadata
- report_missing_credential: called when a needed credential is absent
"""

from __future__ import annotations

import logging
from typing import Any

from ..vault import get_vault

logger = logging.getLogger(__name__)

# The vault web page URL (non-secret — safe to return in chat)
VAULT_URL = "/vault"


def get_vault_url() -> str:
    """Return the vault web page URL.

    The vault URL is non-secret — it can be shared with anyone.
    Access to the vault itself is gated by email→RSA authentication
    and the Governors cache.

    Returns:
        The vault URL path.
    """
    return VAULT_URL


def check_credential(name: str) -> dict[str, Any]:
    """Check if a credential exists in the vault and return its metadata.

    Never returns the credential value — only metadata (name, purpose,
    scopes, version, created_by, created_at).

    Args:
        name: The credential name to check.

    Returns:
        A dict with credential metadata, or an error dict if not found.
    """
    try:
        vault = get_vault()
        if not vault.is_initialized():
            return {
                "found": False,
                "error": "Vault is not initialized.",
                "vault_url": VAULT_URL,
            }

        if not vault.has_credential(name):
            return {
                "found": False,
                "error": f"Credential '{name}' not found in vault.",
                "vault_url": VAULT_URL,
            }

        ref = vault.get_ref(name)
        return {
            "found": True,
            "name": ref.name,
            "purpose": ref.purpose,
            "scopes": ref.scopes,
            "version": ref.version,
            "created_by": ref.created_by,
            "created_at": ref.created_at,
            "note": "Value is never returned through chat. Use the vault web page to manage credentials.",
            "vault_url": VAULT_URL,
        }
    except Exception as e:
        logger.error("Failed to check credential '%s': %s", name, e)
        return {
            "found": False,
            "error": f"Failed to access vault: {e}",
            "vault_url": VAULT_URL,
        }


def report_missing_credential(name: str, purpose: str) -> str:
    """Report that a needed credential is missing from the vault.

    Called by tools when they need a credential that doesn't exist.
    Never fails silently — always tells the governor what's missing
    and how to fix it.

    Args:
        name: The credential name that's missing.
        purpose: What the credential is needed for.

    Returns:
        A message for the governor explaining the gap.
    """
    vault = get_vault()
    initialized = vault.is_initialized()

    if not initialized:
        return (
            f"⚠️ The credential vault has not been initialized yet. "
            f"I need a credential named **{name}** for **{purpose}**, "
            f"but the vault doesn't exist yet.\n\n"
            f"Please visit the vault page at {VAULT_URL} to initialize it "
            f"and add the **{name}** credential."
        )

    if vault.has_credential(name):
        return (
            f"The credential **{name}** exists in the vault. "
            f"If it's not working, try rotating it on the vault page: {VAULT_URL}"
        )

    return (
        f"⚠️ I need a credential named **{name}** for **{purpose}**, "
        f"but it's not in the vault.\n\n"
        f"Please add it on the vault page: {VAULT_URL}\n\n"
        f"Once added, I'll be able to use it for {purpose}."
    )
