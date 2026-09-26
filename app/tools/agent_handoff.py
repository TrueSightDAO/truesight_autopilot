"""Agent-to-agent handoff tools — a generic, pull-based mailbox.

Any registered agent (Sophia, Bionpact, or a future sibling instance) can
hand information to any other registered agent without either needing write
access to the other's own private storage. Every agent gets one narrow write
grant to a single shared, purpose-built repo (TrueSightDAO/agent_handoffs)
instead of access to each other's context/transcript/attachment repos.

Convention (see TrueSightDAO/agent_handoffs/handoffs/README.md):
- Registry: agentic_ai_context/agents/<name>.json lists known agents.
- Delivery: one file per handoff, in agent_handoffs/handoffs/, named
  "{target}_from_{sender}_{iso8601-timestamp}.json".
- Retrieval: pull-based — each agent lists the shared repo's handoffs/
  folder for files prefixed with its own name. No inbound network access
  needed by either party (this is what lets a network-isolated instance
  like Bionpact participate).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from .github_tools import read_repo_file
from .upload_file_to_github import upload_file_to_github

logger = logging.getLogger("autopilot.tools.agent_handoff")

_HANDOFFS_REPO = "agent_handoffs"
_HANDOFFS_DIR = "handoffs"


def _registry_lookup(agent_name: str) -> dict[str, Any] | None:
    """Best-effort read of agentic_ai_context/agents/<name>.json — used only
    to validate the target is a known agent and surface a friendlier error
    for a typo'd name. Never blocks the actual handoff on failure (the
    shared repo location doesn't depend on the registry lookup succeeding)."""
    try:
        result = read_repo_file("agentic_ai_context", f"agents/{agent_name}.json")
        if result.get("type") == "file":
            return json.loads(result.get("content", "{}"))
    except Exception as e:  # noqa: BLE001 — best-effort, never blocks the handoff
        logger.warning("agent registry lookup failed for %s: %s", agent_name, e)
    return None


def send_handoff(
    target_agent: str,
    summary: str,
    context: str = "",
    thread_id: str = "",
) -> str:
    """Hand information off to another registered agent.

    Writes a single JSON file into the shared TrueSightDAO/agent_handoffs
    repo — the receiving agent picks it up on its own schedule via
    check_handoffs(). This instance's own identity (the "from" field) comes
    from settings.agent_name.

    Args:
        target_agent: The receiving agent's registered name (e.g. "bionpact",
            "sophia") — must match its agents/<name>.json registry entry.
        summary: One-line summary of what's being handed off.
        context: Fuller context/details for the receiving agent.
        thread_id: Optional — the originating Telegram thread, if relevant.

    Returns:
        JSON string with status and the handoff file's URL.
    """
    if not target_agent or not summary:
        return json.dumps(
            {"status": "error", "reason": "target_agent and summary are required"}
        )
    if target_agent == settings.agent_name:
        return json.dumps(
            {"status": "error", "reason": "target_agent cannot be this instance itself"}
        )

    registry_entry = _registry_lookup(target_agent)
    if registry_entry is None:
        logger.warning(
            "send_handoff: %s not found in agent registry — sending anyway "
            "(best-effort lookup, not a hard gate)",
            target_agent,
        )

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    filename = (
        f"{_HANDOFFS_DIR}/{target_agent}_from_{settings.agent_name}_{timestamp}.json"
    )

    payload = {
        "from": settings.agent_name,
        "to": target_agent,
        "timestamp": now.isoformat(),
        "summary": summary,
        "context": context,
    }
    if thread_id:
        payload["thread_id"] = thread_id

    result = upload_file_to_github(
        repo=_HANDOFFS_REPO,
        path=filename,
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        message=f"[handoff] {settings.agent_name} -> {target_agent}: {summary[:80]}",
    )
    if result.get("status") != "success":
        return json.dumps(
            {"status": "error", "reason": result.get("error", "upload failed")}
        )

    return json.dumps(
        {
            "status": "success",
            "to": target_agent,
            "file": filename,
            "url": result.get("content_url", ""),
        }
    )


def check_handoffs() -> str:
    """List and read handoffs addressed to this instance.

    Pull-based — lists TrueSightDAO/agent_handoffs' handoffs/ folder for
    files prefixed with this instance's own agent name (settings.agent_name)
    and returns each one's contents. Fold anything relevant into your own
    context/notes after reading (v1 has no automatic "mark as read" — the
    same handoff will surface again on the next check_handoffs() call).

    Returns:
        JSON string: {"status", "count", "handoffs": [...]}.
    """
    listing = read_repo_file(_HANDOFFS_REPO, _HANDOFFS_DIR)
    if listing.get("type") == "error":
        return json.dumps(
            {"status": "error", "reason": listing.get("error", "list failed")}
        )
    if listing.get("type") != "directory":
        return json.dumps({"status": "success", "count": 0, "handoffs": []})

    prefix = f"{settings.agent_name}_from_"
    mine = [
        e for e in listing.get("entries", []) if e.get("name", "").startswith(prefix)
    ]

    handoffs = []
    for entry in mine:
        file_result = read_repo_file(_HANDOFFS_REPO, entry["path"])
        if file_result.get("type") != "file":
            continue
        try:
            handoffs.append(json.loads(file_result.get("content", "{}")))
        except json.JSONDecodeError:
            logger.warning(
                "check_handoffs: %s is not valid JSON, skipping", entry["path"]
            )

    handoffs.sort(key=lambda h: h.get("timestamp", ""))
    return json.dumps(
        {"status": "success", "count": len(handoffs), "handoffs": handoffs}
    )


# ── capability manifest entries ─────────────────────────────────────────

from ..tool_registry import ToolSpec

TOOL_SPECS = [
    ToolSpec(
        name="send_handoff",
        description=(
            "Hand information off to another registered agent instance (e.g. Sophia "
            "handing something to Bionpact, or vice versa) — writes to a shared mailbox "
            "repo the receiving agent will check on its own; does not require write "
            "access to the other agent's own storage. Use when a governor asks you to "
            "pass context/information to another autopilot instance, or when you "
            "recognize something the other instance should know about."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_agent": {
                    "type": "string",
                    "description": "The receiving agent's registered name (e.g. 'bionpact', 'sophia').",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line summary of what's being handed off.",
                },
                "context": {
                    "type": "string",
                    "description": "Fuller context/details for the receiving agent.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional — the originating Telegram thread, if relevant.",
                },
            },
            "required": ["target_agent", "summary"],
        },
        handler=lambda args, ctx: send_handoff(
            args.get("target_agent", ""),
            args.get("summary", ""),
            args.get("context", ""),
            args.get("thread_id", ""),
        ),
    ),
    ToolSpec(
        name="check_handoffs",
        description=(
            "Check for information other agent instances have handed off to you. Call "
            "this when a governor asks 'did Sophia/Bionpact send anything over?' or "
            "periodically to see if another instance has passed something along."
        ),
        parameters={"type": "object", "properties": {}},
        handler=lambda args, ctx: check_handoffs(),
    ),
]
