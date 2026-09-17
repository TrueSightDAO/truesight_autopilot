"""Pin the ``gh`` CLI to the canonical DAO GitHub PAT.

The autopilot's own tools authenticate with ``settings.github_pat`` (env
``TRUESIGHT_DAO_AUTOPILOT``) through the GitHub REST client. The ``gh`` CLI
ignores that variable: it reads ``GH_TOKEN`` first and, failing that, the
token stored in ``~/.config/gh/hosts.yml``. On a box where ``hosts.yml``
holds a *different* -- usually under-scoped -- fine-grained PAT, ad-hoc
``gh`` calls then fail with::

    403 Resource not accessible by personal access token

even though every tool call succeeds. All PATs on a box commonly
authenticate as the *same* user, so ``gh auth status`` looks healthy and the
mismatch is easy to miss (see ``credentials/API_CREDENTIALS_DOCUMENTATION.md``
section 10.2.2). This module closes that gap at service start.

Token values are never logged: only the length and a short one-way
fingerprint are emitted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOSTNAME = "github.com"
_TOKEN_LINE = re.compile(r"^(\s*(?:oauth_token|token):\s*)(\S+)(\s*)$")


def _fingerprint(token: str) -> str:
    """Short, non-reversible fingerprint -- safe to log."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def gh_hosts_path() -> Path:
    """Location of the ``gh`` CLI credentials file (honours ``GH_CONFIG_DIR``)."""
    override = os.getenv("GH_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".config" / "gh"
    return base / "hosts.yml"


def export_gh_token(token: str) -> str:
    """Export ``GH_TOKEN`` from ``token`` so ``gh`` subprocesses inherit it.

    Returns ``no-token`` / ``already-set`` / ``exported`` / ``updated``.
    """
    if not token:
        return "no-token"
    existing = os.environ.get("GH_TOKEN")
    if existing == token:
        return "already-set"
    os.environ["GH_TOKEN"] = token
    return "updated" if existing else "exported"


def reconcile_hosts_file(
    hosts_path: Path, token: str, hostname: str = DEFAULT_HOSTNAME
) -> str:
    """Point the ``hostname`` block's token at ``token`` (timestamped backup first).

    Line-level edit so unrelated hosts and keys survive untouched. Returns one
    of ``absent`` / ``already-correct`` / ``no-token-line`` / ``reconciled`` /
    ``error``.
    """
    if not token or not hosts_path.exists():
        return "absent"
    try:
        lines = hosts_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("Could not read %s: %s", hosts_path, exc)
        return "error"

    in_host = False
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not line[:1].isspace() and stripped.endswith(":"):
            in_host = stripped[:-1] == hostname
        if in_host:
            match = _TOKEN_LINE.match(line.rstrip("\n"))
            if match:
                if match.group(2) == token:
                    return "already-correct"
                out.append(f"{match.group(1)}{token}\n")
                replaced = True
                in_host = False
                continue
        out.append(line)

    if not replaced:
        return "no-token-line"

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = hosts_path.parent / f"{hosts_path.name}.bak.{stamp}"
        shutil.copy2(hosts_path, backup)
        tmp = hosts_path.parent / f"{hosts_path.name}.tmp"
        tmp.write_text("".join(out), encoding="utf-8")
        os.replace(tmp, hosts_path)
    except OSError as exc:
        logger.warning("Could not reconcile %s: %s", hosts_path, exc)
        return "error"
    logger.info("gh hosts.yml reconciled (backup: %s)", backup.name)
    return "reconciled"


def ensure_gh_cli_uses_canonical_pat(
    settings: Any = None, hostname: str = DEFAULT_HOSTNAME
) -> dict[str, str]:
    """Make the ``gh`` CLI use the canonical DAO PAT. Never raises."""
    if settings is None:
        from app.config import settings as _settings

        settings = _settings

    token = getattr(settings, "github_pat", "") or ""
    result = {
        "env": export_gh_token(token),
        "hosts": (
            "skipped"
            if not token
            else reconcile_hosts_file(gh_hosts_path(), token, hostname)
        ),
        "fingerprint": _fingerprint(token) if token else "",
    }
    if not token:
        logger.warning(
            "github_pat is empty -- gh CLI left untouched (a locked-down "
            "instance may intentionally have no write PAT)"
        )
    else:
        logger.info(
            "gh CLI pinned to canonical PAT (env=%s hosts=%s len=%d fp=%s)",
            result["env"],
            result["hosts"],
            len(token),
            result["fingerprint"],
        )
    return result


def _main() -> int:
    """Doctor entry point: ``python -m app.gh_cli [--check]``."""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Pin gh CLI to the canonical DAO PAT")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the current state without changing anything",
    )
    args = parser.parse_args()

    if args.check:
        from app.config import settings as _settings

        token = getattr(_settings, "github_pat", "") or ""
        hosts = gh_hosts_path()
        print(f"hosts file: {hosts} ({'present' if hosts.exists() else 'absent'})")
        print(f"GH_TOKEN in env: {bool(os.getenv('GH_TOKEN'))}")
        print(
            f"github_pat set: {bool(token)} fp={_fingerprint(token) if token else '-'}"
        )
        return 0

    print(ensure_gh_cli_uses_canonical_pat())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
