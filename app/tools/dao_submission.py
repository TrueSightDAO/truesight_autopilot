"""Phase 2 tool: programmatic DAO submission via Edgar."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def compute_tdg_from_rubric(contribution_type: str, amount: str) -> str | None:
    """Derive TDG from the canonical dao_client rubric (single source of truth).

    Returns the rubric value as a 2dp string (e.g. ``600`` min of
    ``Time (Minutes)`` -> ``"1000.00"``), or ``None`` when it cannot be derived
    (unknown type or non-numeric amount). Callers MUST treat ``None`` as "let the
    downstream CLI compute it", never as a reason to hand-zero the award.

    Rationale: Gary's standing rule (2026-09-10, thread 24442, documented in
    ``agentic_ai_context/dao/DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md`` rule 5) --
    always use the auto-computed rubric TDG (100 TDG per Time hour, 1:1 for
    USD/USDT); never default it to 0 by hand.
    """
    try:
        from truesight_dao_client.rubric import format_tdg, tdg_for
    except Exception:
        return None
    try:
        return format_tdg(tdg_for(contribution_type, amount))
    except Exception:
        return None


def submit_ai_agent_contribution(
    title: str,
    body: str,
    pr_urls: list[str],
    contributors: str | None = None,
    amount: str = "0",
    contribution_type: str = "Time (Minutes)",
    tdg_issued: str | None = None,
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
    cmd.extend(["--type", contribution_type])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    cmd.extend(["--body-file", body_file])

    for url in pr_urls:
        cmd.extend(["--pr", url])

    if contributors:
        cmd.extend(["--contributors", contributors])
    # The dao_client CLI derives BOTH Amount and TDG itself from --hours/
    # --minutes/--usd via the rubric SSOT; it does not accept --amount or
    # --tdg-issued. Pass the amount in the shape it expects so the TDG is
    # auto-computed per the rubric rather than silently hand-zeroed.
    if contribution_type in ("USD", "USDT sent", "USDT received"):
        cmd.extend(["--usd", amount])
    else:
        cmd.extend(["--minutes", amount])
    if generation_source:
        cmd.extend(["--generation-source", generation_source])
    if attached_file_path:
        cmd.extend(["--attachment", attached_file_path])
    if attached_filename:
        cmd.extend(["--attached-filename", attached_filename])
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(dao_client_dir) if use_module else None,
    )

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
        description="Submit a signed [CONTRIBUTION EVENT] or other event to Edgar (the DAO API). Supports CONTRIBUTOR ADD EVENT, INVENTORY MOVEMENT, SALES EVENT, QR CODE REGISTRATION, and more.",
        parameters={
            "type": "object",
            "properties": {
                "event_name": {
                    "type": "string",
                    "description": "Event name, e.g. 'CONTRIBUTION EVENT', 'INVENTORY MOVEMENT', 'CONTRIBUTOR ADD EVENT'.",
                },
                "attributes": {
                    "type": "object",
                    "description": "Key-value pairs describing the event.",
                },
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
                "pr_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "PR URLs as evidence.",
                },
                "contributors": {"type": "string", "description": "Display name."},
                "amount": {
                    "type": "string",
                    "description": "Minutes or USD amount.",
                    "default": "0",
                },
                "type": {
                    "type": "string",
                    "description": "Contribution type: 'Time (Minutes)' or 'USD'. Defaults to 'Time (Minutes)'.",
                    "default": "Time (Minutes)",
                },
                "tdg_issued": {
                    "type": "string",
                    "description": (
                        "TDG to issue. Leave UNSET to use the auto-computed "
                        "rubric value (100 TDG per Time hour; 1:1 for USD) - "
                        "this is the default and preferred. Only set this to "
                        "override the rubric with a governor-approved figure."
                    ),
                },
                "attachment_path": {
                    "type": "string",
                    "description": "Local file path to attach (e.g. /tmp/tg_attachments/receipt.pdf). File is uploaded to GitHub via Edgar.",
                },
                "attachment_filename": {
                    "type": "string",
                    "description": "Override the auto-generated attachment filename.",
                },
            },
            "required": ["title", "body", "pr_urls"],
        },
        handler=None,  # dispatched inline in main.py (uses governor_name from session)
    ),
]
