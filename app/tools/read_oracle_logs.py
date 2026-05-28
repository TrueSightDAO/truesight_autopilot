"""Tool to read oracle draw logs from the oracle_logs repo."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("autopilot.oracle_logs")

ORACLE_LOGS_BASE = "https://raw.githubusercontent.com/TrueSightDAO/oracle_logs/main/draws"


def read_oracle_logs(date: str | None = None) -> str:
    """Read oracle draw logs from TrueSightDAO/oracle_logs.

    If date is None, returns a listing of available draws.
    If date is "latest", fetches the most recent draw.
    Otherwise, fetches draws/YYYY-MM-DD.md.

    Returns JSON string with status and content.
    """
    try:
        if date is None:
            # List available draws via GitHub API
            url = "https://api.github.com/repos/TrueSightDAO/oracle_logs/contents/draws"
            resp = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=10.0)
            if resp.status_code != 200:
                return json.dumps({"status": "error", "message": f"GitHub API error: {resp.status_code}"})
            files = resp.json()
            if not isinstance(files, list) or len(files) == 0:
                return json.dumps({"status": "ok", "draws": [], "message": "No draws found"})
            names = [f["name"].replace(".md", "") for f in files if f.get("name", "").endswith(".md")]
            names.sort(reverse=True)
            return json.dumps({"status": "ok", "draws": names, "message": f"{len(names)} draws available"})

        if date == "latest":
            # Find the latest draw
            url = "https://api.github.com/repos/TrueSightDAO/oracle_logs/contents/draws"
            resp = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=10.0)
            if resp.status_code != 200:
                return json.dumps({"status": "error", "message": f"GitHub API error: {resp.status_code}"})
            files = resp.json()
            if not isinstance(files, list) or len(files) == 0:
                return json.dumps({"status": "ok", "content": "", "message": "No draws found"})
            names = [f["name"] for f in files if f.get("name", "").endswith(".md")]
            if not names:
                return json.dumps({"status": "ok", "content": "", "message": "No draws found"})
            names.sort(reverse=True)
            date = names[0].replace(".md", "")

        # Fetch specific draw
        url = f"{ORACLE_LOGS_BASE}/{date}.md"
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code != 200:
            return json.dumps({"status": "error", "message": f"Draw not found: {date}"})

        return json.dumps({
            "status": "ok",
            "date": date,
            "content": resp.text,
            "message": f"Oracle draw for {date} retrieved"
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="read_oracle_logs",
    description="Read oracle draw logs from TrueSightDAO/oracle_logs.",
    parameters={
        "type": "object",
        "properties": {"date": {"type": "string", "description": "YYYY-MM-DD date, 'latest', or omit to list draws.", "default": "latest"}},
    },
    handler=lambda args, ctx: read_oracle_logs(date=args.get("date")),
)
