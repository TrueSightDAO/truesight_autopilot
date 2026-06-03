"""Schema + role-gating entries for orchestration tools that stay inline.

The actual dispatch for these tools happens in ``app/main.py:_run_tool`` —
they touch session state (history, approval gate, _add_pending persistence)
that the generic registry handler signature doesn't carry yet. Until a future
PR migrates them with a uniform context-dict pattern, this module just
publishes their JSON schemas so the LLM sees them and role-gating works.
"""
from __future__ import annotations

from ..config import settings
from ..tool_registry import ToolSpec

# Single source of truth — main.py enforces against settings.allowed_repos,
# so the schema's advertised list must come from the same place (it used to
# be a hand-copied list that drifted; see OPEN_FOLLOW_UPS item 3).
_ALLOWED_CHAT_REPOS = ", ".join(settings.allowed_repos)

TOOL_SPECS = [
    ToolSpec(
        name="open_fix_pr",
        description="Run a full agentic loop to diagnose and fix an issue in any TrueSightDAO repo. Opens a DRAFT PR.",
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": f"Repo name under TrueSightDAO. Allowed: {_ALLOWED_CHAT_REPOS}"},
                "issue_description": {"type": "string", "description": "Description of the issue to fix."},
            },
            "required": ["repo", "issue_description"],
        },
        handler=None,  # dispatched inline in app/main.py (FixAgent.run_simple + _add_pending)
    ),
]
