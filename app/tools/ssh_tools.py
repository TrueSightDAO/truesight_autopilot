"""SSH tool: run commands on the TrueSight DAO / Krake EC2 fleet.

Sophia's outbound SSH capability. The host registry mirrors
``agentic_ai_context/AWS_DIGITAL_INFRASTRUCTURE.md`` §2 (EC2 inventory) and
§7 (SSH access) — update BOTH when the fleet changes.

Auth: dedicated ``sophia_infra`` ed25519 keypair (independently revocable —
grep the key comment in each host's ``authorized_keys``). The private key is
synced to the box by ``scripts/deploy.sh``; the public key is distributed to
the fleet by ``scripts/distribute_sophia_ssh_key.sh`` (operator-run).

Guardrails:
- known-host registry only (no arbitrary IPs),
- ``BatchMode=yes`` — never hangs on a password prompt,
- timeouts + output truncation.
This is an SRE power tool: prefer reading logs / checking services; for code
changes still go through git_push_changes / open_fix_pr + PR review.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.tools.ssh_tools")

_DEFAULT_KEY_PATH = "~/.ssh/sophia_infra"
_DEFAULT_TIMEOUT_SECS = 60
_MAX_TIMEOUT_SECS = 300
_MAX_OUTPUT_CHARS = 8000

# Mirrors AWS_DIGITAL_INFRASTRUCTURE.md §2 — running hosts only.
# label → (public IP, user, what it is)
FLEET: dict[str, dict[str, str]] = {
    "krake_nginx": {
        "ip": "54.226.114.186", "user": "ubuntu", "port": "2202",
        "desc": "Nginx reverse proxy — terminates HTTPS for edgar/api/chatbot.truesight.me (Nelanco)",
    },
    "seni_ror": {
        "ip": "54.211.179.126", "user": "ubuntu", "port": "22",
        "desc": "Edgar (Rails sentiment_importer) — DAO API server (Nelanco, seni_ror_200250915)",
    },
    "dao_protocol": {
        "ip": "98.93.94.86", "user": "ubuntu", "port": "22",
        "desc": "dao_protocol FastAPI server, port 8010 (Nelanco)",
    },
    "seni_sk": {
        "ip": "34.234.193.80", "user": "ubuntu", "port": "22",
        "desc": "Sidekiq worker for Edgar (Nelanco, seni_sk_auto)",
    },
    "seni_sql": {
        "ip": "44.193.55.205", "user": "ubuntu", "port": "22",
        "desc": "PostgreSQL for Edgar (Nelanco, seni_sql_2026)",
    },
    "seni_redis": {
        "ip": "54.234.59.188", "user": "ubuntu", "port": "22",
        "desc": "Redis for Edgar Sidekiq/cache (Nelanco, seni_redis_2)",
    },
    "krake_ror": {
        "ip": "18.205.20.43", "user": "ubuntu", "port": "22",
        "desc": "Krake Rails backend, getdata.io (Nelanco)",
    },
    "krake_sk": {
        "ip": "54.227.147.20", "user": "ubuntu", "port": "22",
        "desc": "Krake Sidekiq worker (Nelanco)",
    },
    "krake_sk_webhook": {
        "ip": "52.207.88.236", "user": "ubuntu", "port": "22",
        "desc": "Krake webhook worker (Nelanco)",
    },
    "krake_sk_crawler": {
        "ip": "52.91.57.12", "user": "ubuntu", "port": "22",
        "desc": "Krake crawler worker (Nelanco)",
    },
    "krake_sk_scaler": {
        "ip": "100.25.41.96", "user": "ubuntu", "port": "22",
        "desc": "Krake autoscaling worker (Nelanco)",
    },
    "krake_data": {
        "ip": "52.5.179.48", "user": "ubuntu", "port": "22",
        "desc": "Krake data processing (Nelanco)",
    },
    "getdata_redis": {
        "ip": "52.1.162.134", "user": "ubuntu", "port": "22",
        "desc": "Redis for Krake (Nelanco, GETDATA_REDIS)",
    },
    "getdata_cache": {
        "ip": "98.84.169.188", "user": "ubuntu", "port": "22",
        "desc": "Krake cache worker (Nelanco, GETDATA_CACHE)",
    },
}


def _err(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "reason": reason, **extra}


def _key_path() -> Path:
    """Return the first existing SSH key from a list of candidates.
    Falls back through: sophia_infra -> id_ed25519_truesight_autopilot -> id_rsa."""
    env_key = os.environ.get("SOPHIA_SSH_KEY_PATH", "")
    if env_key:
        p = Path(env_key).expanduser()
        if p.is_file():
            return p
    candidates = [
        Path(_DEFAULT_KEY_PATH).expanduser(),
        Path.home() / ".ssh/id_ed25519_truesight_autopilot",
        Path.home() / ".ssh/GETDATA_IO_PAIR_20201122",
        Path.home() / ".ssh/id_rsa",
        Path.home() / ".ssh/id_ed25519",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]  # return default even if missing, for the error message


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s, False
    return s[-_MAX_OUTPUT_CHARS:], True


def ssh_run(host: str, command: str, timeout_secs: int = _DEFAULT_TIMEOUT_SECS) -> dict[str, Any]:
    """Run ``command`` on a fleet host over SSH; return rc/stdout/stderr."""
    if not host or not command:
        return _err("host and command are required")
    spec = FLEET.get(host)
    if spec is None:
        return _err(
            "unknown host — pick one from the fleet registry",
            host=host,
            fleet={k: v["desc"] for k, v in FLEET.items()},
        )
    key = _key_path()
    if not key.is_file():
        tried = [
            str(Path(_DEFAULT_KEY_PATH).expanduser()),
            str(Path.home() / ".ssh/id_ed25519_truesight_autopilot"),
            str(Path.home() / ".ssh/id_rsa"),
        ]
        return _err(
            f"No SSH key found — tried: {', '.join(tried)}. "
            "Generate one via the /tools/generate-ssh-key endpoint, then add the public key "
            "to the target host's ~/.ssh/authorized_keys.",
        )
    timeout = max(5, min(int(timeout_secs or _DEFAULT_TIMEOUT_SECS), _MAX_TIMEOUT_SECS))

    port = spec.get("port", "22")
    cmd = [
        "ssh",
        "-i", str(key),
        "-p", port,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
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


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="ssh_run",
    description=(
        "Run a shell command on a TrueSight DAO / Krake production EC2 host "
        "over SSH and get rc/stdout/stderr back. Hosts: "
        + "; ".join(f"'{k}' = {v['desc']}" for k, v in FLEET.items())
        + ". Use for SRE diagnostics and service operations (journalctl, "
        "systemctl status/restart, df, free, tail logs). For code changes "
        "still open a PR (git_push_changes / open_fix_pr) — do not hand-edit "
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
            "command": {"type": "string", "description": "Shell command to run on the host."},
            "timeout_secs": {
                "type": "integer",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT_SECS}, max {_MAX_TIMEOUT_SECS}).",
            },
        },
        "required": ["host", "command"],
    },
    handler=lambda args, ctx: json.dumps(ssh_run(
        host=args.get("host", ""),
        command=args.get("command", ""),
        timeout_secs=args.get("timeout_secs", _DEFAULT_TIMEOUT_SECS),
    ), indent=2),
    default_roles=frozenset({"infrastructure"}),
)
