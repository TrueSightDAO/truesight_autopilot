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
    attached_file_path: str | None = None,
    attached_filename: str | None = None,
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
    if attached_file_path:
        cmd.extend(["--attachment", attached_file_path])
    if attached_filename:
        cmd.extend(["--attached-filename", attached_filename])
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


# ── capability manifest entries (orchestration; handlers stay inline) ────

from ..tool_registry import ToolSpec  # noqa: E402

# These two tools have session-state side effects (history check, _add_pending
# pending-approval persistence, governor metadata injection) handled inline in
# app/main.py:_run_tool. The TOOL_SPEC declarations below put the schemas +
# role-gating under the manifest, but the registry's dispatcher leaves
# handler=None so the existing inline branch takes over. Future PR can move
# the handlers here once we have a uniform context-dict pattern.
TOOL_SPECS = [
    ToolSpec(
        name="submit_contribution",
        description="Submit a signed [CONTRIBUTION EVENT] or other event to Edgar (the DAO API).",
        parameters={
            "type": "object",
            "properties": {
                "event_name": {"type": "string", "description": "Event name, e.g. 'CONTRIBUTION EVENT', 'INVENTORY MOVEMENT'."},
                "attributes": {"type": "object", "description": "Key-value pairs describing the event."},
            },
            "required": ["event_name", "attributes"],
        },
        handler=None,  # dispatched inline in main.py (history/approval-gate orchestration)
    ),
    ToolSpec(
        name="create_dao_submission",
        description="Submit a [CONTRIBUTION EVENT] to Edgar for DAO contribution tracking. Optionally attach a local file (PDF, image, etc.) that will be uploaded to GitHub and linked in the submission.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short one-line title."},
                "body": {"type": "string", "description": "Multi-line description."},
                "pr_urls": {"type": "array", "items": {"type": "string"}, "description": "PR URLs as evidence."},
                "contributors": {"type": "string", "description": "Display name."},
                "amount": {"type": "string", "description": "Minutes or dollar amount.", "default": "0"},
                "tdg_issued": {"type": "string", "description": "TDG to issue.", "default": "0"},
                "attachment_path": {"type": "string", "description": "Local file path to attach (e.g. /tmp/tg_attachments/receipt.pdf). File is uploaded to GitHub via Edgar."},
                "attachment_filename": {"type": "string", "description": "Override the auto-generated attachment filename."},
            },
            "required": ["title", "body", "pr_urls"],
        },
        handler=None,  # dispatched inline in main.py (uses governor_name from session)
    ),
]
