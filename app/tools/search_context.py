"""Content search across the synced agentic_ai_context repository.

Born from a real failure (2026-06-06): a governor asked about "email360" —
a term that appears 10+ times inside GROWTH_MODEL.md — and the agent reported
"not in my context" because it could only list filenames and read known paths.
Filename listings are not a search. This tool greps file CONTENTS so a
governor's vocabulary can be resolved before declaring something unknown.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import settings
from ..context import context_read_lock
from ..tool_registry import ToolSpec

_TEXT_EXTS = {".md", ".json", ".yml", ".yaml", ".txt", ".csv", ".html", ".py", ".js", ".gs", ".sh", ".template"}
_MAX_FILE_BYTES = 2_000_000
_SNIPPET_LEN = 220


def _context_dir() -> Path | None:
    candidates = [
        settings.context_repos_dir / "agentic_ai_context",
        Path(__file__).resolve().parent.parent.parent.parent / "agentic_ai_context",
        Path.home() / "Applications" / "agentic_ai_context",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def search_context(query: str, max_results: int = 30) -> dict[str, Any]:
    """Case-insensitive content search across all text files in agentic_ai_context.

    Returns matches as {file, line, snippet} plus a per-file rollup so the
    model can decide which file to read_context_file next.
    """
    query = (query or "").strip()
    if not query:
        return {"status": "error", "message": "Empty query."}

    root = _context_dir()
    if root is None:
        return {"status": "error", "message": "agentic_ai_context clone not found on this host."}

    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error as e:  # pragma: no cover — escape() makes this unreachable
        return {"status": "error", "message": f"Bad query: {e}"}

    matches: list[dict[str, Any]] = []
    files_hit: dict[str, int] = {}
    truncated = False

    # Hold the shared (cross-process) read lock for the whole walk so the tree
    # can't be reset (git reset --hard) underneath us mid-traversal — otherwise a
    # file could be half-written or vanish between rglob() and read_text().
    # Bounded: the repo is small and the walk is fast; the writer only contends
    # during its sub-second reset. See context_read_lock / context_write_lock.
    with context_read_lock():
        for path in sorted(root.rglob("*")):
            if len(matches) >= max_results:
                truncated = True
                break
            if not path.is_file() or path.suffix.lower() not in _TEXT_EXTS:
                continue
            rel = str(path.relative_to(root))
            if rel.startswith(".git/") or "/node_modules/" in rel:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    files_hit[rel] = files_hit.get(rel, 0) + 1
                    if len(matches) < max_results:
                        snippet = line.strip()
                        if len(snippet) > _SNIPPET_LEN:
                            cut = pattern.search(snippet)
                            start = max(0, (cut.start() if cut else 0) - 60)
                            snippet = ("…" if start else "") + snippet[start : start + _SNIPPET_LEN] + "…"
                        matches.append({"file": rel, "line": lineno, "snippet": snippet})
                    else:
                        truncated = True

    return {
        "status": "ok",
        "query": query,
        "match_count": len(matches),
        "truncated": truncated,
        "files": [{"file": f, "hits": n} for f, n in sorted(files_hit.items(), key=lambda kv: -kv[1])],
        "matches": matches,
    }


def _search_context_handler(args: dict, ctx: dict) -> str:
    result = search_context(
        args.get("query", ""),
        max_results=int(args.get("max_results", 30) or 30),
    )
    return json.dumps(result, indent=2)


TOOL_SPEC = ToolSpec(
    name="search_context",
    description=(
        "Search the CONTENTS of every file in agentic_ai_context for a term or phrase "
        "(case-insensitive). Use this FIRST whenever a governor mentions a name, project, "
        "tab, tool, or loop you don't recognize — terms often live inside docs whose "
        "filenames don't mention them (e.g. 'Email360' lives in GROWTH_MODEL.md). "
        "Returns file/line/snippet matches plus a per-file hit rollup; follow up with "
        "read_context_file on the best file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Term or phrase to search for (literal, case-insensitive)."},
            "max_results": {"type": "integer", "description": "Max line matches to return (default 30)."},
        },
        "required": ["query"],
    },
    handler=_search_context_handler,
)
