"""read_tool_result — fetch a tool result that was offloaded to an artifact.

Context-management Pillar A (CONTEXT_MANAGEMENT_PLAN.md): large tool results are
written to a local artifact file and only an 8K summary stays inline. This tool
reads the full output back (or a slice) on demand, so Sophia conserves context yet
never loses detail.
"""

from __future__ import annotations

from ..tool_registry import ToolSpec


def _read(artifact_id, session_id, offset=0, limit=16000):
    if not artifact_id:
        return {"status": "error", "reason": "artifact_id is required"}
    if not session_id:
        return {
            "status": "error",
            "reason": "no session context to resolve the artifact",
        }
    from ..main import _read_artifact  # lazy import — avoid circular at module load

    content = _read_artifact(artifact_id, session_id, offset, limit)
    return {"status": "ok", "artifact_id": artifact_id, "content": content}


TOOL_SPEC = ToolSpec(
    name="read_tool_result",
    description=(
        "Read the FULL output of an earlier tool result that was offloaded to an "
        "artifact. When a tool result is large, its inline summary ends with "
        "\"saved to artifact '<id>'\" — pass that id here to see the complete output "
        "(e.g. the truncated tail of a big ssh / sheet / http result). Only needed "
        "when the inline 8K summary isn't enough. Optional offset/limit to page "
        "through very large results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "The artifact id from the tool-result summary.",
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to start reading from (default 0).",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return (default 16000).",
            },
        },
        "required": ["artifact_id"],
    },
    handler=lambda args, ctx: _read(
        args.get("artifact_id", ""),
        (ctx or {}).get("session_id", ""),
        args.get("offset", 0),
        args.get("limit", 16000),
    ),
)
