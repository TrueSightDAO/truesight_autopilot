"""Autopilot tool: deploy a single GAS project via tokenomics/scripts/deploy_gas_project.py.

Thin wrapper around the manifest-driven deploy script that landed in
[tokenomics#323](https://github.com/TrueSightDAO/tokenomics/pull/323).
The script itself is the canonical implementation; this tool just exposes
it to the LLM tool-call surface so a governor can ask "deploy
`tdg_credentialing`" over Telegram and have autopilot run the full
sync → `clasp push` → post-push-hooks flow with the right safety flags.

Conservative by default:
- `push=False` (dry-run) unless the caller explicitly opts in.
- `with_hooks=False` even when `push=True`, so the first real-world
  deploy fires `clasp push` but skips firing `post_push_hooks[]` until
  the governor confirms the push landed correctly.
- The tool refuses to run if the tokenomics checkout isn't present on
  the host (autopilot's EC2 box needs the same clone as a developer
  laptop). The error message points at the env var to set.

Runtime requirements (autopilot host) — all provisioned automatically by
`scripts/deploy.sh` since 2026-06-03:
- `tokenomics` cloned at `GAS_DEPLOY_TOKENOMICS_ROOT` (default
  `/opt/truesight_autopilot/context/tokenomics`) — deploy.sh clones/refreshes.
- `node` + `clasp` installed and on PATH — deploy.sh installs Node 20 +
  clasp 3.3.0 (also baked into user-data.sh for fresh boxes).
- clasp auth — deploy.sh syncs the operator Mac's `~/.clasprc.json`
  (clasp login is interactive OAuth; the token file is the portable artifact).

If any of those aren't ready, the tool surfaces the failure verbatim
rather than masking it — so the operator can react.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.tools.gas_deploy_project")

DEFAULT_TOKENOMICS_ROOT = "/opt/truesight_autopilot/context/tokenomics"
DEPLOY_SCRIPT_REL = "scripts/deploy_gas_project.py"
# Cap on stdout/stderr returned to the model — clasp push output can be
# verbose; we want enough to debug but not enough to drown the LLM.
_MAX_OUTPUT_CHARS = 8000
# Deploys can be slow — clasp push of a multi-file project + a couple of
# cache-refresh hooks routinely takes 30-60s.
_DEFAULT_TIMEOUT_SECS = 300


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _resolve_clasp_oauth() -> str | None:
    """Vault-first: try clasp_oauth_gary, fall back to ~/.clasprc-gary.json."""
    try:
        from ..vault import Vault
        v = Vault()
        if v.is_initialized():
            v.initialize()
            val = v.get_value("clasp_oauth_gary")
            if val:
                # Write to a temp .clasprc.json for clasp to find
                clasp_path = Path.home() / ".clasprc.json"
                clasp_path.write_text(val)
                clasp_path.chmod(0o600)
                logger.info("Resolved clasp OAuth from vault -> ~/.clasprc.json")
                return val
    except Exception:
        logger.debug("vault clasp OAuth lookup failed, falling back")
    # Fallback: check for gary-specific clasp file
    gary_clasp = Path.home() / ".clasprc-gary.json"
    if gary_clasp.is_file():
        import shutil
        shutil.copy2(gary_clasp, Path.home() / ".clasprc.json")
        logger.info("Fell back to .clasprc-gary.json")
        return gary_clasp.read_text()
    return None


def _resolve_tokenomics_root() -> Path | None:
    """Find a local tokenomics checkout the deploy script can run from."""
    candidates = [
        os.environ.get("GAS_DEPLOY_TOKENOMICS_ROOT", "").strip(),
        DEFAULT_TOKENOMICS_ROOT,
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if (p / DEPLOY_SCRIPT_REL).is_file():
            return p
    return None


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s, False
    return s[:_MAX_OUTPUT_CHARS], True


def gas_deploy_project(
    script_id: str,
    push: bool = False,
    with_hooks: bool = False,
    timeout_secs: int = _DEFAULT_TIMEOUT_SECS,
) -> str:
    """Run `scripts/deploy_gas_project.py <script_id> [--push] [--with-hooks]`.

    Returns a JSON-string tool result:
    {
      "status": "ok" | "error",
      "exit_code": int,
      "tokenomics_root": str,
      "command": [argv...],
      "stdout": str,
      "stderr": str,
      "stdout_truncated": bool,
      "stderr_truncated": bool,
      "push": bool,
      "with_hooks": bool,
    }
    """
    if not script_id or not isinstance(script_id, str):
        return _err("script_id is required (string)")

    # Ensure clasp OAuth is resolved before deploy
    _resolve_clasp_oauth()

    root = _resolve_tokenomics_root()
    if root is None:
        return _err(
            "tokenomics checkout not found on this host",
            checked=[
                os.environ.get("GAS_DEPLOY_TOKENOMICS_ROOT", "(unset)"),
                DEFAULT_TOKENOMICS_ROOT,
            ],
            fix=(
                "Clone TrueSightDAO/tokenomics under "
                "/opt/truesight_autopilot/context/tokenomics (or set "
                "GAS_DEPLOY_TOKENOMICS_ROOT in the autopilot service env to "
                "point at an existing checkout)."
            ),
        )

    cmd = ["python3", str(root / DEPLOY_SCRIPT_REL), script_id]
    if push:
        cmd.append("--push")
        if with_hooks:
            cmd.append("--with-hooks")
        else:
            cmd.append("--no-hooks")
    # else: dry-run (no flags)

    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired as e:
        return _err(
            "deploy script timed out",
            timeout_secs=timeout_secs,
            command=cmd,
            stdout=(e.stdout or "")[-2000:] if isinstance(e.stdout, str) else "",
        )
    except FileNotFoundError as e:
        return _err(f"failed to invoke deploy script: {e}", command=cmd)

    stdout, stdout_truncated = _truncate(result.stdout or "")
    stderr, stderr_truncated = _truncate(result.stderr or "")

    payload: dict[str, Any] = {
        "status": "ok" if result.returncode == 0 else "error",
        "exit_code": result.returncode,
        "tokenomics_root": str(root),
        "command": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "push": bool(push),
        "with_hooks": bool(push and with_hooks),
    }
    logger.info(
        "gas_deploy_project: script_id=%s push=%s with_hooks=%s exit=%d",
        script_id,
        push,
        with_hooks,
        result.returncode,
    )
    return json.dumps(payload)


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="gas_deploy_project",
    description=(
        "Deploy a single Google Apps Script project end-to-end via the "
        "tokenomics `scripts/deploy_gas_project.py` script. Reads "
        "`google_app_scripts/<theme>/manifest.json` to figure out which "
        "source files to sync into `clasp_mirrors/<scriptId>/`, runs "
        "`clasp push`, then fires `post_push_hooks[]`. "
        "**Dry-run by default** (no GAS change). "
        "Pass `push=true` to actually push. Pass `with_hooks=true` "
        "(only with `push=true`) to also fire promoted post-push hooks "
        "— candidate cache-refresh hooks are NEVER fired automatically. "
        "Use `--list`-style introspection by calling with a fake scriptId "
        "first to see the dry-run shape. Operator must have a tokenomics "
        "checkout, `clasp`, and `clasp login` already set up on the host. "
        "Returns the full deploy script stdout/stderr so the model can "
        "diagnose what happened."
    ),
    parameters={
        "type": "object",
        "properties": {
            "script_id": {
                "type": "string",
                "description": "The GAS scriptId (the long ID under `clasp_mirrors/<scriptId>/`).",
            },
            "push": {
                "type": "boolean",
                "description": "Actually `clasp push` (default false = dry-run).",
                "default": False,
            },
            "with_hooks": {
                "type": "boolean",
                "description": "Also fire promoted `post_push_hooks[]` (only with push=true). Default false — first deploy should push without hooks, confirm, then re-run with hooks.",
                "default": False,
            },
            "timeout_secs": {
                "type": "integer",
                "description": "Timeout for the deploy script. Default 300.",
                "default": 300,
            },
        },
        "required": ["script_id"],
    },
    handler=lambda args, ctx: gas_deploy_project(
        script_id=args.get("script_id", ""),
        push=bool(args.get("push", False)),
        with_hooks=bool(args.get("with_hooks", False)),
        timeout_secs=int(args.get("timeout_secs") or _DEFAULT_TIMEOUT_SECS),
    ),
)
