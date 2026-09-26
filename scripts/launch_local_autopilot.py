#!/usr/bin/env python3
"""Double-fork daemon launcher for a local autopilot instance.

Detaches from the controlling terminal so the process survives the
spawning shell exiting (the failure mode that blocked Bundle B's local
testing on macOS without `setsid`).

Usage:
    python scripts/launch_local_autopilot.py [--port 8011] [--log /tmp/autopilot.log]

The integration tests under `tests/integration/` default to
http://127.0.0.1:8011, so the default port matches.

To stop:
    pkill -f 'uvicorn.*<port>'
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8011
DEFAULT_LOG = "/tmp/autopilot_8011.log"


def daemonize(log_path: str) -> None:
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    os.chdir(str(REPO_ROOT))
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", "rb") as devnull_in:
        os.dup2(devnull_in.fileno(), 0)
    with open(log_path, "ab", buffering=0) as logf:
        os.dup2(logf.fileno(), 1)
        os.dup2(logf.fileno(), 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if not venv_python.exists():
        sys.stderr.write(
            f"FATAL: {venv_python} not found. Run `python -m venv .venv && pip install -r requirements.txt` first.\n"
        )
        sys.exit(1)

    daemonize(args.log)
    os.execv(
        str(venv_python),
        [
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
    )


if __name__ == "__main__":
    main()
