"""Shared Google service-account credential loading for the autopilot tools.

Runtime convention
------------------
All service-account JSONs live in a single directory on disk. By default that
directory is ``/opt/truesight_autopilot/config/google`` on the EC2 host (and
``<repo>/config/google`` locally during development). Each file follows the
naming convention ``<service_account_name>_gdrive_key.json`` (or
``<service_account_name>_key.json`` for non-drive ones, e.g.
``edgar_dapp_listener_key.json``).

Selection precedence:
1. If the caller passes ``service_account_name`` (e.g. ``"tdg_scoring"``),
   look for ``{name}_gdrive_key.json`` then ``{name}_key.json`` in
   ``GOOGLE_CREDS_DIR``.
2. Otherwise fall back to the path in ``GOOGLE_APPLICATION_CREDENTIALS``.
3. If neither resolves to an existing file, return ``None`` — tools surface
   this as a ``{status: "error", reason: "credentials missing"}`` result rather
   than raising at import time, so the service still boots when the files
   haven't been provisioned yet.

This module deliberately exposes ``load_credentials()`` (returning a
``google.oauth2.service_account.Credentials`` or ``None``) instead of building
API client objects — the per-tool modules build their own clients with the
scopes they need.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("autopilot.tools.google_creds")

DEFAULT_CREDS_DIR = "/opt/truesight_autopilot/config/google"


def _candidate_paths(service_account_name: str | None) -> list[Path]:
    """Ordered list of paths to try when resolving a credential file."""
    out: list[Path] = []
    creds_dir = os.environ.get("GOOGLE_CREDS_DIR", DEFAULT_CREDS_DIR)
    if service_account_name:
        out.append(Path(creds_dir) / f"{service_account_name}_gdrive_key.json")
        out.append(Path(creds_dir) / f"{service_account_name}_key.json")
    env_default = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if env_default:
        out.append(Path(env_default))
    return out


def resolve_credentials_path(service_account_name: str | None = None) -> str | None:
    """Return the first existing credential path, or ``None``."""
    for p in _candidate_paths(service_account_name):
        if p.is_file():
            return str(p)
    return None


def load_credentials(
    service_account_name: str | None = None,
    scopes: Iterable[str] | None = None,
):
    """Load a service-account ``Credentials`` instance.

    Returns ``None`` when no credential file resolves — callers should turn
    that into an error-shaped tool result rather than crashing.
    """
    # Lazy import: keeps service startup fast and avoids hard-fail if the
    # google-auth dep ever drifts.
    try:
        from google.oauth2 import service_account  # type: ignore
    except Exception as e:  # pragma: no cover — dep is pinned in requirements
        logger.error("google-auth not available: %s", e)
        return None

    path = resolve_credentials_path(service_account_name)
    if not path:
        logger.warning(
            "No Google credentials file found (service_account_name=%s, GOOGLE_APPLICATION_CREDENTIALS=%s, GOOGLE_CREDS_DIR=%s)",
            service_account_name,
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
            os.environ.get("GOOGLE_CREDS_DIR", DEFAULT_CREDS_DIR),
        )
        return None
    try:
        return service_account.Credentials.from_service_account_file(path, scopes=list(scopes or []))
    except Exception as e:
        logger.error("Failed to load Google credentials from %s: %s", path, e)
        return None
