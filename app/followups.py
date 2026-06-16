"""
Durable follow-up registry — parser + state sidecar.

Parses ```followup blocks from OPEN_FOLLOWUPS.md (leaves all prose
untouched), manages mutable scheduling state in followups/state.json,
and provides atomic read/write access for the follow-up comb loop.

Schema (the fenced block in .md):

```followup
id: matheus-nota-fiscal
chat_id: -1003919341801
thread_id: 10
title: Chase Matheus for the Nota Fiscal
created_at: 2026-06-11
condition:
  kind: gmail_reply
  from: matheus@example.com
  subject_contains: Nota Fiscal
schedule:
  check: daily
  escalate_after_days: 2
  on_escalate: ping_thread
status: open
```

"""

from __future__ import annotations

import os
import re
import tempfile
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── paths ────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_followups_md() -> Path:
    """Locate OPEN_FOLLOWUPS.md across the known agentic_ai_context layouts.

    Mirrors app.context.get_context_file: the configured context mirror first
    (the box syncs the repo to ``<context_repos_dir>/agentic_ai_context``, e.g.
    ``/opt/truesight_autopilot/context/agentic_ai_context``), then a clone next
    to this checkout, then the developer's ``~/Applications`` clone. Falls back
    to the configured location so a missing-file error points at the canonical
    path. Resolving against the live config (rather than a hard-coded
    ``<repo>/agentic_ai_context``) is what stopped /vault/followups 500-ing on
    the box, where the repo isn't a sibling of this checkout.
    """
    from .config import settings

    candidates = [
        settings.context_repos_dir / "agentic_ai_context",
        _REPO_ROOT / "agentic_ai_context",
        _REPO_ROOT.parent / "agentic_ai_context",
        Path.home() / "Applications" / "agentic_ai_context",
    ]
    for c in candidates:
        if (c / "OPEN_FOLLOWUPS.md").exists():
            return c / "OPEN_FOLLOWUPS.md"
    return settings.context_repos_dir / "agentic_ai_context" / "OPEN_FOLLOWUPS.md"


_FOLLOWUPS_MD = _resolve_followups_md()
_STATE_DIR = _REPO_ROOT / "followups"
_STATE_FILE = _STATE_DIR / "state.json"

# ── regex ────────────────────────────────────────────────────────────────

_FOLLOWUP_BLOCK_RE = re.compile(
    r"^```followup\n(?P<body>.*?)\n```$",
    re.MULTILINE | re.DOTALL,
)


# ── public helpers ───────────────────────────────────────────────────────


def _ensure_state_dir() -> None:
    """Create followups/ directory if it doesn't exist."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, Any]:
    """Load mutable scheduling state from disk. Returns {} on first run."""
    if not _STATE_FILE.exists():
        return {}
    raw = _STATE_FILE.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    """Atomically write state to disk (tmp + os.replace)."""
    _ensure_state_dir()
    fd, tmp_path = tempfile.mkstemp(dir=_STATE_DIR, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _STATE_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


def _read_md() -> str:
    """Read the current OPEN_FOLLOWUPS.md content."""
    return _FOLLOWUPS_MD.read_text(encoding="utf-8")


def _write_md(content: str) -> None:
    """Write OPEN_FOLLOWUPS.md atomically."""
    fd, tmp_path = tempfile.mkstemp(dir=_FOLLOWUPS_MD.parent, suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _FOLLOWUPS_MD)
    except BaseException:
        os.unlink(tmp_path)
        raise


# ── parsing ──────────────────────────────────────────────────────────────


def _parse_block(body: str, line_offset: int) -> dict[str, Any] | str:
    """
    Parse a single ```followup block body into a dict.

    Returns the dict on success, or an error string on failure.
    """
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError as e:
        return f"YAML parse error at line ~{line_offset}: {e}"

    if not isinstance(parsed, dict):
        return f"Followup block at line ~{line_offset} is not a mapping"

    # Required fields
    for field in ("id", "chat_id", "thread_id", "title", "created_at", "status"):
        if field not in parsed:
            return (
                f"Followup '{parsed.get('id', '?')}' missing required field '{field}'"
            )

    # thread_id is REQUIRED — this is the guardrail against silent background loops
    if not parsed.get("thread_id"):
        return f"Followup '{parsed.get('id', '?')}' missing required field 'thread_id'"

    # Normalise status
    status = parsed.get("status", "open").strip().lower()
    if status not in ("open", "resolved", "aborted"):
        return f"Followup '{parsed['id']}' has invalid status '{status}'"
    parsed["status"] = status

    return parsed


def parse_all() -> list[dict[str, Any]]:
    """
    Parse all ```followup blocks from OPEN_FOLLOWUPS.md.

    Returns a list of parsed dicts. Blocks that fail validation are
    skipped with a warning printed to stderr.
    """
    content = _read_md()
    results: list[dict[str, Any]] = []

    for match in _FOLLOWUP_BLOCK_RE.finditer(content):
        # Estimate line number for error messages
        line_offset = content[: match.start()].count("\n") + 1
        body = match.group("body")
        parsed = _parse_block(body, line_offset)
        if isinstance(parsed, str):
            print(f"[followups] WARNING: {parsed}")
            continue
        results.append(parsed)

    return results


def get(id: str) -> dict[str, Any] | None:
    """Get a single follow-up by id from the .md definition."""
    for f in parse_all():
        if f["id"] == id:
            return f
    return None


def list_open() -> list[dict[str, Any]]:
    """Return all follow-ups with status=open."""
    return [f for f in parse_all() if f.get("status") == "open"]


# ── state sidecar ────────────────────────────────────────────────────────


def upsert_state(id: str, **kwargs: Any) -> dict[str, Any]:
    """
    Update mutable scheduling state for a follow-up.

    Merges kwargs into the state entry. Creates a new entry with defaults
    if one doesn't exist. Returns the full state entry.
    """
    state = _load_state()
    entry = state.get(
        id,
        {
            "id": id,
            "last_checked": None,
            "next_check": None,
            "attempts": 0,
            "last_pinged": None,
        },
    )
    entry.update(kwargs)
    state[id] = entry
    _write_state(state)
    return entry


def get_state(id: str) -> dict[str, Any] | None:
    """Get mutable state for a follow-up. Returns None if never seen."""
    state = _load_state()
    return state.get(id)


def set_status(id: str, new_status: str) -> bool:
    """
    Set a follow-up's status (open | resolved | aborted).

    Updates BOTH the .md block AND the state sidecar.
    For resolved/aborted, moves the block under the appropriate heading.
    Returns True on success, False if the id wasn't found.
    """
    new_status = new_status.strip().lower()
    if new_status not in ("open", "resolved", "aborted"):
        raise ValueError(f"Invalid status: {new_status}")

    content = _read_md()

    # Find the block
    pattern = re.compile(
        r"^```followup\nid: " + re.escape(id) + r".*?\n```$",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        return False

    block_text = match.group(0)

    # Update status in the block body
    updated_block = re.sub(
        r"^status: .*$",
        f"status: {new_status}",
        block_text,
        count=1,
        flags=re.MULTILINE,
    )

    if new_status == "open":
        # Just update in place
        new_content = content[: match.start()] + updated_block + content[match.end() :]
    elif new_status == "resolved":
        # Move to ## Recently shipped
        new_content = content[: match.start()] + content[match.end() :]
        # Find the Recently shipped section and append
        shipped_marker = "\n## Recently shipped\n"
        shipped_idx = new_content.find(shipped_marker)
        if shipped_idx >= 0:
            # Insert after the section header (find the next blank line or end)
            insert_point = shipped_idx + len(shipped_marker)
            new_content = (
                new_content[:insert_point]
                + "\n"
                + updated_block
                + "\n\n"
                + new_content[insert_point:]
            )
        else:
            # No Recently shipped section — append at end
            new_content = (
                new_content.rstrip()
                + "\n\n## Recently shipped\n\n"
                + updated_block
                + "\n"
            )
    else:
        # aborted — move to ## Closed without shipping
        new_content = content[: match.start()] + content[match.end() :]
        closed_marker = "\n## Closed without shipping\n"
        closed_idx = new_content.find(closed_marker)
        if closed_idx >= 0:
            insert_point = closed_idx + len(closed_marker)
            new_content = (
                new_content[:insert_point]
                + "\n"
                + updated_block
                + "\n\n"
                + new_content[insert_point:]
            )
        else:
            new_content = (
                new_content.rstrip()
                + "\n\n## Closed without shipping\n\n"
                + updated_block
                + "\n"
            )

    _write_md(new_content)

    # Update state sidecar
    upsert_state(id, status=new_status)

    return True


# ── convenience ──────────────────────────────────────────────────────────


def next_due(now: datetime | None = None) -> list[dict[str, Any]]:
    """
    Return open follow-ups whose next_check is due (or None if never checked).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    open_followups = list_open()
    state = _load_state()
    due: list[dict[str, Any]] = []

    for f in open_followups:
        entry = state.get(f["id"])
        if entry is None:
            # Never checked — due immediately
            due.append(f)
            continue
        next_check = entry.get("next_check")
        if next_check is None:
            due.append(f)
            continue
        try:
            check_dt = datetime.fromisoformat(next_check)
            if check_dt <= now:
                due.append(f)
        except (ValueError, TypeError):
            due.append(f)

    return due


import json  # noqa: E402 — must be at module level after all defs
