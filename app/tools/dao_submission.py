"""Phase 2 tool: programmatic DAO submission via Edgar."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import settings


def submit_ai_agent_contribution(
    title: str,
    body: str,
    pr_urls: list[str],
    contributors: str | None = None,
    amount: str = "0",
    tdg_issued: str = "0",
    generation_source: str | None = None,
    dry_run: bool = False,
) -> dict:
    workspace_root = Path(__file__).resolve().parents[3]
    dao_client_dir = workspace_root / "dao_client"

    entry_point = "truesight-dao-report-ai-agent-contribution"
    use_module = False

    try:
        subprocess.run([entry_point, "--help"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        use_module = True

    cmd: list[str]
    env = os.environ.copy()

    if use_module:
        cmd = [
            sys.executable,
            "-m",
            "truesight_dao_client.modules.report_ai_agent_contribution",
        ]
        env["PYTHONPATH"] = str(dao_client_dir)
    else:
        cmd = [entry_point]

    cmd.extend(["--title", title])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    cmd.extend(["--body-file", body_file])

    for url in pr_urls:
        cmd.extend(["--pr", url])

    if contributors:
        cmd.extend(["--contributors", contributors])
    cmd.extend(["--amount", amount])
    cmd.extend(["--tdg-issued", tdg_issued])
    if generation_source:
        cmd.extend(["--generation-source", generation_source])
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(dao_client_dir) if use_module else None)

    try:
        os.unlink(body_file)
    except Exception:
        pass

    return {
        "status": "success" if result.returncode == 0 else "error",
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
