"""Context-management Pillar C tools — recall + working-set.

- search_transcript: the "really look back" recall — keyword-greps this session's
  FULL on-disk history + artifacts and returns matched spans, so Sophia can stay on
  a recent window by default yet pull older context on demand.
- pin_note: pin a short working-set note (active goal / key decision) that survives
  the token-trim and sub-task compaction (it's stored as a [PINNED] system message,
  which neither drops).
"""

from __future__ import annotations

from ..tool_registry import ToolSpec


def _search(query, session_id, max_spans=8):
    if not session_id:
        return {"status": "error", "reason": "no session context to search"}
    from ..main import _search_transcript  # lazy — avoid circular import

    try:
        max_spans = int(max_spans or 8)
    except (TypeError, ValueError):
        max_spans = 8
    return _search_transcript(query, session_id, max(1, min(max_spans, 25)))


def _pin(text, history):
    text = (text or "").strip()
    if not text:
        return {"status": "error", "reason": "text is required"}
    if history is None:
        return {"status": "error", "reason": "no session context to pin into"}
    note = "[PINNED] " + text
    for m in history:
        if m.get("role") == "system" and m.get("content") == note:
            return {"status": "ok", "pinned": text, "note": "already pinned"}
    history.append({"role": "system", "content": note})
    return {"status": "ok", "pinned": text}


SEARCH_SPEC = ToolSpec(
    name="recall_context",
    description=(
        "Search THIS conversation's full history + offloaded tool-result artifacts "
        "for a keyword and return the matching spans. Use when the user says 'really "
        "look back' / asks about something from much earlier in this thread than the "
        "recent context — the live context only holds recent turns, so this is how "
        "you recall older detail from the current conversation. (For files a governor "
        "previously attached, use search_transcript instead.)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword / phrase to search for (case-insensitive).",
            },
            "max_spans": {
                "type": "integer",
                "description": "Max matching spans to return (default 8).",
            },
        },
        "required": ["query"],
    },
    handler=lambda args, ctx: _search(
        args.get("query", ""),
        (ctx or {}).get("session_id", ""),
        args.get("max_spans", 8),
    ),
)

PIN_SPEC = ToolSpec(
    name="pin_note",
    description=(
        "Pin a short working-set note — the active goal or a key decision — so it "
        "stays in context permanently (it survives the history trim and sub-task "
        "compaction). Use for things you must not forget across a long thread, e.g. "
        "'Decision: perch goes on seni_ror, not krake_ng'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The note to pin (keep it short).",
            },
        },
        "required": ["text"],
    },
    handler=lambda args, ctx: _pin(args.get("text", ""), (ctx or {}).get("history")),
)

TOOL_SPECS = [SEARCH_SPEC, PIN_SPEC]
