"""SSH tool: run commands on the TrueSight DAO / Krake EC2 fleet.

Vault-first: each fleet host resolves the SSH identity it actually trusts.
Three encrypted vault credentials cover the fleet (see
SOPHIA_VAULT_CREDENTIAL_MIGRATION_PLAN.md):

  ssh_key_nelanco_aws         - Krake/Seni Nelanco fleet (RSA)
  ssh_key_server_us           - Krake core US-East hosts (RSA)
  ssh_key_nelanco_california  - californian_proxy (RSA)

Two hosts trust the box-local ``sophia_infra`` ed25519 key instead and pin it
via ``FLEET[host]["key"]`` (this autopilot box itself, and ``dao_protocol``).
Unpinned hosts keep the vault-first host-agnostic default. The host registry
mirrors AWS_DIGITAL_INFRASTRUCTURE.md sections 2 and 7.

Guardrails: known-host registry only; BatchMode=yes (never hangs on a password
prompt); timeouts + output truncation. SRE power tool -- for code changes go
through git_push_changes / open_fix_pr + PR review.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.tools.ssh_tools")

_DEFAULT_KEY_PATH = "~/.ssh/sophia_infra"
_DEFAULT_TIMEOUT_SECS = 60
_MAX_TIMEOUT_SECS = 300
_MAX_OUTPUT_CHARS = 8000

# Block raw self-restart of the autopilot: it bypasses deploy_autopilot's
# idle-drain guard and bricks in-flight turns. The alternatives are split across
# lines on purpose so THIS source file does not itself match the pattern.
_SELF_RESTART_RE = re.compile(
    r"(systemctl|service)\s+"
    r"(restart|stop|kill|reload)\b[^\n]*"
    r"truesight[-_]autopilot"
    r"|(pkill|killall)\b[^\n]*"
    r"(uvicorn|app\.main|truesight[-_]autopilot)"
    r"|kill\b[^\n]*"
    r"\b(uvicorn|app\.main)\b",
    re.IGNORECASE,
)

# Vault credential -> equivalent on-box key file. Used ONLY when the vault is
# unavailable (CI / a fresh box before deploy.sh seeds it). These PEMs are the
# migration source for the vault entries.
_VAULT_KEY_FILE_FALLBACK = {
    "ssh_key_nelanco_aws": "~/.ssh/NELANCO_aws_20201122.pem",
    "ssh_key_server_us": "~/.ssh/server_us.pem",
    "ssh_key_nelanco_california": "~/.ssh/NELANCO_california_20260213.pem",
}

# Vault-first order for the host-agnostic DEFAULT key. server_us leads because it
# is the historical default -- hosts without a per-host pin resolve exactly as
# before this change (no regression); the others are reached only if it is gone.
_DEFAULT_VAULT_KEYS = (
    "ssh_key_server_us",
    "ssh_key_nelanco_aws",
    "ssh_key_nelanco_california",
)

FLEET: dict[str, dict[str, str]] = {
    "autopilot": {
        # THIS box -- loopback so the entry survives IP changes / blue-green AMI
        # swaps. Self-trust: deploy.sh + user-data.sh add sophia_infra.pub to
        # this box's own authorized_keys.
        "ip": "127.0.0.1",
        "user": "ubuntu",
        "desc": "THIS autopilot box itself (Sophia's own host) -- loopback self-exec for package installs / sudo on her own machine",
    },
    "krake_nginx": {
        # No vault pin: trusts server_us and has no dedicated vault key.
        "ip": "54.226.114.186",
        "user": "ubuntu",
        "port": "2202",
        "desc": "Nginx reverse proxy -- terminates HTTPS for edgar/api/chatbot.truesight.me (Nelanco)",
    },
    "seni_ror": {
        "ip": "54.211.179.126",
        "user": "ubuntu",
        "desc": "Rails sentiment_importer -- trading platform ONLY, NOT the DAO API (dao_protocol handles DAO on its own box)",
    },
    "dao_protocol": {
        "ip": "98.93.94.86",
        "user": "ubuntu",
        "desc": "dao_protocol FastAPI server, port 8010 (Nelanco)",
        # Pinned: trusts the box-local sophia_infra key, NOT the vault default.
        "key": "~/.ssh/sophia_infra",
    },
    "seni_sk": {
        "ip": "34.234.193.80",
        "user": "ubuntu",
        "desc": "Sidekiq worker for Edgar (Nelanco, seni_sk_auto)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "seni_sql": {
        "ip": "44.193.55.205",
        "user": "ubuntu",
        "desc": "PostgreSQL for Edgar (Nelanco, seni_sql_2026)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "seni_redis": {
        "ip": "54.234.59.188",
        "user": "ubuntu",
        "desc": "Redis for Edgar Sidekiq/cache (Nelanco, seni_redis_2)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "krake_ror": {
        "ip": "18.205.20.43",
        "user": "ubuntu",
        "desc": "Krake Rails backend, getdata.io (Nelanco)",
        "vault_key": "ssh_key_server_us",
    },
    "krake_sk": {
        "ip": "54.227.147.20",
        "user": "ubuntu",
        "desc": "Krake Sidekiq worker (Nelanco)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "krake_sk_webhook": {
        "ip": "52.207.88.236",
        "user": "ubuntu",
        "desc": "Krake webhook worker (Nelanco)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "krake_sk_crawler": {
        "ip": "52.91.57.12",
        "user": "ubuntu",
        "desc": "Krake crawler worker (Nelanco)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "krake_sk_scaler": {
        "ip": "100.25.41.96",
        "user": "ubuntu",
        "desc": "Krake autoscaling worker (Nelanco)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "krake_data": {
        "ip": "52.5.179.48",
        "user": "ubuntu",
        "desc": "Krake data processing (Nelanco)",
        "vault_key": "ssh_key_server_us",
    },
    "getdata_redis": {
        "ip": "52.1.162.134",
        "user": "ubuntu",
        "desc": "Redis for Krake (Nelanco, GETDATA_REDIS)",
        "vault_key": "ssh_key_nelanco_aws",
    },
    "getdata_cache": {
        "ip": "98.84.169.188",
        "user": "ubuntu",
        "desc": "Krake cache worker (Nelanco, GETDATA_CACHE)",
        "vault_key": "ssh_key_nelanco_aws",
    },
}


def _err(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "reason": reason, **extra}


def _vault_credential(name: str) -> str | None:
    """Return a vault credential's decrypted value, or ``None``.

    Uses the shared ``get_vault()`` singleton so the ``VAULT_DIR`` env override
    (honoured by the hermetic test suite) applies. Value is never logged.
    """
    try:
        from ..vault import get_vault

        vault = get_vault()
        if vault.is_initialized() and vault.has_credential(name):
            return vault.get_value(name)
    except Exception:
        logger.debug("vault lookup failed for %s, falling back", name)
    return None


def _write_temp_key(key_material: str) -> Path:
    """Persist SSH key material to a 0600 temp file and return its path."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix="sophia_sshkey_", suffix=".pem")
    with os.fdopen(fd, "w") as fh:
        fh.write(key_material)
    Path(path).chmod(0o600)
    return Path(path)


def _resolve_named_key(vault_name: str) -> Path | None:
    """Resolve one named vault credential to a usable key file.

    Vault first, then the equivalent on-box file fallback
    (``_VAULT_KEY_FILE_FALLBACK``). Returns ``None`` when neither is available.
    """
    material = _vault_credential(vault_name)
    if material:
        return _write_temp_key(material)
    fallback = _VAULT_KEY_FILE_FALLBACK.get(vault_name)
    if fallback:
        path = Path(fallback).expanduser()
        if path.is_file():
            return path
    return None


def _resolve_by_vault_key(host: str) -> Path | None:
    """Resolve a host's identity from its per-host ``FLEET[host]['vault_key']``."""
    vault_key = (FLEET.get(host) or {}).get("vault_key")
    if not vault_key:
        return None
    path = _resolve_named_key(vault_key)
    if path is None:
        logger.warning("no key available for %s (vault_key=%s)", host, vault_key)
    return path


def _key_path() -> Path:
    """Vault-first host-agnostic default identity, with a file fallback."""
    for vault_name in _DEFAULT_VAULT_KEYS:
        path = _resolve_named_key(vault_name)
        if path is not None:
            logger.info("Resolved default SSH key from %s", vault_name)
            return path
    env_key = os.environ.get("SOPHIA_SSH_KEY_PATH", "")
    if env_key:
        p = Path(env_key).expanduser()
        if p.is_file():
            return p
    candidates = [
        Path(_DEFAULT_KEY_PATH).expanduser(),
        Path.home() / ".ssh/id_ed25519_truesight_autopilot",
        Path.home() / ".ssh/id_rsa",
        Path.home() / ".ssh/id_ed25519",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def _identity_for(host: str) -> Path:
    """Resolve the SSH identity for a specific fleet host.

    1. A host-local file pin -- ``FLEET[host]['key']`` (e.g. ~/.ssh/sophia_infra).
    2. A vault pin -- ``FLEET[host]['vault_key']`` names the credential the host
       actually trusts. THIS is the fix: the old host-agnostic ``_key_path()``
       handed every host the same server_us key, so hosts trusting only the
       Nelanco key answered "Permission denied (publickey)".
    3. The vault-first host-agnostic default (``_key_path()``).

    Pins are skipped when their material is unavailable, so a missing pin never
    hard-fails a host.
    """
    spec = FLEET.get(host) or {}

    pinned = spec.get("key")
    if pinned:
        p = Path(pinned).expanduser()
        if p.is_file():
            return p
        logger.warning(
            "pinned key %s for host %s missing -- falling back", pinned, host
        )

    resolved = _resolve_by_vault_key(host)
    if resolved is not None:
        return resolved

    return _key_path()


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s, False
    return s[-_MAX_OUTPUT_CHARS:], True


def ssh_run(
    host: str, command: str, timeout_secs: int = _DEFAULT_TIMEOUT_SECS
) -> dict[str, Any]:
    """Run command on a fleet host over SSH; return rc/stdout/stderr."""
    if not host or not command:
        return _err("host and command are required")
    if _SELF_RESTART_RE.search(command):
        return _err(
            "BLOCKED: restarting the autopilot bypasses the idle-drain guard "
            "and bricks active threads (severs in-flight turns + wedges the "
            "adapter). Use the deploy_autopilot tool instead -- it waits for "
            "threads to be idle, then restarts safely. Never cycle the autopilot "
            "service by hand.",
            command=command[:200],
        )
    spec = FLEET.get(host)
    if spec is None:
        return _err(
            "unknown host -- pick one from the fleet registry",
            host=host,
            fleet={k: v["desc"] for k, v in FLEET.items()},
        )
    key = _identity_for(host)
    if not key.is_file():
        tried = [
            str(Path(_DEFAULT_KEY_PATH).expanduser()),
            str(Path.home() / ".ssh/id_ed25519_truesight_autopilot"),
            str(Path.home() / ".ssh/id_rsa"),
        ]
        return _err(
            f"No SSH key found -- tried: {', '.join(tried)}. "
            "Generate one via the /tools/generate-ssh-key endpoint, then add the "
            "public key to the target host's ~/.ssh/authorized_keys.",
        )
    timeout = max(5, min(int(timeout_secs or _DEFAULT_TIMEOUT_SECS), _MAX_TIMEOUT_SECS))

    port = spec.get("port", "22")
    cmd = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        port,
        f"{spec['user']}@{spec['ip']}",
        command,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _err(f"command timed out after {timeout}s", host=host)
    except OSError as e:
        return _err(f"ssh invocation failed: {e}", host=host)

    stdout, out_trunc = _truncate(r.stdout or "")
    stderr, err_trunc = _truncate(r.stderr or "")
    logger.info("ssh_run: host=%s rc=%s cmd=%r", host, r.returncode, command[:120])
    return {
        "status": "ok" if r.returncode == 0 else "nonzero_exit",
        "host": host,
        "ip": spec["ip"],
        "returncode": r.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_trunc or err_trunc,
    }


# --- capability manifest entry -------------------------------------------

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="ssh_run",
    description=(
        "Run a shell command on a TrueSight DAO / Krake production EC2 host "
        "over SSH and get rc/stdout/stderr back. Hosts: "
        + "; ".join(f"'{k}' = {v['desc']}" for k, v in FLEET.items())
        + ". Use for SRE diagnostics and service operations (journalctl, "
        "systemctl status/restart, df, free, tail logs). For code changes "
        "still open a PR (git_push_changes / open_fix_pr) -- do not hand-edit "
        "deployed code over SSH."
    ),
    parameters={
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "Fleet host label.",
                "enum": sorted(FLEET.keys()),
            },
            "command": {
                "type": "string",
                "description": "Shell command to run on the host.",
            },
            "timeout_secs": {
                "type": "integer",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT_SECS}, max {_MAX_TIMEOUT_SECS}).",
            },
        },
        "required": ["host", "command"],
    },
    handler=lambda args, ctx: json.dumps(
        ssh_run(
            host=args.get("host", ""),
            command=args.get("command", ""),
            timeout_secs=args.get("timeout_secs", _DEFAULT_TIMEOUT_SECS),
        ),
        indent=2,
    ),
    default_roles=frozenset({"infrastructure"}),
)
