#!/usr/bin/env python3
"""Vault-resolving remote command probe for cron / ops shell scripts.

Resolves a fleet host's SSH identity through the encrypted credential vault
(vault-first, with the equivalent on-box PEM as a fallback only when the vault
is unavailable) and runs one remote command, printing its stdout.

This is the credential-vault-native replacement for the old hardcoded
``ssh -i /home/ubuntu/.ssh/*.pem`` calls in the df-alert cron scripts. Those
break the moment the bare PEMs are archived
(SOPHIA_VAULT_CREDENTIAL_MIGRATION_PLAN.md Unit 6).

The host registry (ip / user / port / key) lives in
``app/tools/ssh_tools.py::FLEET`` -- single source of truth, so callers stop
drifting when a host moves (e.g. the stale krake_ror IP that the old shell
scripts carried).

Usage:
    fleet_probe.py --host krake_data --command 'df / --output=pcent'
    fleet_probe.py --host seni_ror --command 'df /' --timeout 15

Exit codes: 0 when the remote command succeeds; otherwise the remote return
code (or 3 for a local/registry error). Stdout is the remote stdout verbatim
so shell callers can use it in command substitution.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_LOCAL_ERROR_RC = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one command on a fleet host, resolving its SSH key from the vault."
    )
    parser.add_argument(
        "--host", required=True, help="Fleet host label (see ssh_tools.FLEET)."
    )
    parser.add_argument(
        "--command", required=True, help="Command to run on the remote host."
    )
    parser.add_argument(
        "--timeout", type=int, default=15, help="Timeout in seconds (default 15)."
    )
    args = parser.parse_args(argv)

    from app.tools.ssh_tools import FLEET, ssh_run

    if args.host not in FLEET:
        print(f"fleet_probe: unknown host {args.host!r}", file=sys.stderr)
        return _LOCAL_ERROR_RC

    result = ssh_run(args.host, args.command, timeout_secs=args.timeout)
    status = result.get("status")

    if status == "ok":
        print((result.get("stdout") or "").strip())
        return 0

    if status == "nonzero_exit":
        out = (result.get("stdout") or "").strip()
        if out:
            print(out)
        return int(result.get("returncode") or 1)

    print(f"fleet_probe: {result.get('reason', 'probe failed')}", file=sys.stderr)
    return _LOCAL_ERROR_RC


if __name__ == "__main__":
    raise SystemExit(main())
