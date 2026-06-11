"""Durable follow-up store — parser + state sidecar.

Parses ```followup blocks from OPEN_FOLLOWUPS.md (leaving prose
untouched), manages followups/state.json for mutable scheduling state,
and provides CRUD operations.

Schema (the ```followup block):

    ```followup
    id: matheus-nota-fiscal-aglXX
    chat_id: -1003919341801
    thread_id: 10                         # REQUIRED
    title: Chase Matheus for the Nota Fiscal (AGL-XX)
    created_at: 2026-06-11
    condition:
      kind: gmail_reply
      from: matheus@…
      subject_contains: Nota Fiscal
    schedule:
      check: daily
      escalate_after_days: 2
      on_escalate: ping_thread
    status: open                          # open | resolved | aborted
    ```

State sidecar (followups/state.json):

    {
      "matheus-nota-fiscal-aglXX": {
        "last_checked": "2026-06-11T12:00:00Z",
        "next_check": "2026-06-12T12:00:00Z",
        "attempts": 0,
        "last_pinged": null,
        "status": "open"
      }
    }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Paths ────────────────────────────────────────────────────────────────────

# OPEN_FOLLOWUPS.md lives in agentic_ai_context; we read it from the
# context mirror on the autopilot box. The followups/ sidecar lives
# alongside the session logs so it survives deploys.
_CONTEXT_DIR = Path("/opt/truesight_autopilot/context/agentic_ai_context")
_FOLLOWUPS_MD = _CONTEXT_DIR / "OPEN_FOLLOWUPS.md"

# Sidecar directory — same parent as SESSION_LOG_DIR for deploy survivability.
# Fall back to a local path if the env var is unset.
_SESSION_LOG_DIR = Path(
    os.environ.get("AUTOPILOT_SESSION_LOG_DIR", "/opt/truesight_autopilot/sessions")
)
_FOLLOWUPS_DIR = _SESSION_LOG_DIR.parent / "followups"
_STATE_PATH = _FOLLOWUPS_DIR / "state.json"


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class Followup:
    """A single follow-up parsed from OPEN_FOLLOWUPS.md."""

    id: str
    chat_id: str
    thread_id: str
    title: str
    created_at: str
    condition: dict = field(default_factory=dict)
    schedule: dict = field(default_factory=dict)
    status: str = "open"
    # Raw block text for round-trip preservation
    _raw_block: str = ""
    # Line offset in the .md file (for in-place edits)
    _line_start: int = 0
    _line_end: int = 0


@dataclass
class FollowupState:
    """Mutable scheduling state for a single follow-up."""

    last_checked: str | None = None
    next_check: str | None = None
    attempts: int = 0
    last_pinged: str | None = None
    status: str = "open"


# ── Block regex ──────────────────────────────────────────────────────────────

# Matches a ```followup ... ``` block, capturing the inner YAML-like content
# and the full block (including fences) for round-trip replacement.
_FOLLOWUP_BLOCK_RE = re.compile(
    r"^(?P<fence>```followup)\s*\n"
    r"(?P<body>.*?)\n"
    r"^```\s*$",
    re.MULTILINE | re.DOTALL,
)

# Matches a single key: value line inside the block body
_KEY_VALUE_RE = re.compile(
    r"^(?P<key>[a-zA-Z_][a-zA-Z_0-9]*):\s*(?P<value>.*)$", re.MULTILINE
)

# Matches nested dict lines (indented key: value)
_NESTED_KEY_RE = re.compile(
    r"^\s+(?P<key>[a-zA-Z_][a-zA-Z_0-9]*):\s*(?P<value>.*)$", re.MULTILINE
)


# ── Parsing ──────────────────────────────────────────────────────────────────


def _parse_block_body(body: str) -> dict:
    """Parse the YAML-like body of a ```followup block into a flat dict with
    nested dicts for 'condition' and 'schedule'."""
    result: dict[str, Any] = {}
    current_section: str | None = None
    current_nested: dict[str, str] = {}

    for line in body.split("\n"):
        # Check for section header (a key with no value, followed by indented lines)
        top_match = _KEY_VALUE_RE.match(line)
        if top_match:
            key = top_match.group("key")
            value = top_match.group("value").strip()
            if value == "":
                # This starts a nested section (condition: / schedule:)
                current_section = key
                current_nested = {}
            else:
                current_section = None
                result[key] = value
            continue

        # Check for nested key under a section
        nested_match = _NESTED_KEY_RE.match(line)
        if nested_match and current_section:
            current_nested[nested_match.group("key")] = nested_match.group(
                "value"
            ).strip()
            continue

        # Blank line — end any open section
        if not line.strip():
            if current_section and current_nested:
                result[current_section] = dict(current_nested)
                current_section = None
                current_nested = {}
            continue

    # Flush any remaining nested section
    if current_section and current_nested:
        result[current_section] = dict(current_nested)

    return result


def _block_to_followup(
    block_text: str, body: str, line_start: int, line_end: int
) -> Followup | str:
    """Convert a parsed block into a Followup dataclass.
    Returns the Followup on success, or an error string on failure."""
    parsed = _parse_block_body(body)

    # Required fields
    fid = parsed.get("id", "")
    chat_id = parsed.get("chat_id", "")
    thread_id = parsed.get("thread_id", "")
    title = parsed.get("title", "")
    created_at = parsed.get("created_at", "")
    status = parsed.get("status", "open")

    # Validate: thread_id is REQUIRED
    if not thread_id:
        return f"Parse error in followup '{fid or '(unnamed)'}': 'thread_id' is required but missing or empty"

    condition = parsed.get("condition", {})
    if isinstance(condition, str):
        condition = {}
    schedule = parsed.get("schedule", {})
    if isinstance(schedule, str):
        schedule = {}

    return Followup(
        id=fid,
        chat_id=chat_id,
        thread_id=thread_id,
        title=title,
        created_at=created_at,
        condition=condition,
        schedule=schedule,
        status=status,
        _raw_block=block_text,
        _line_start=line_start,
        _line_end=line_end,
    )


def parse_followups(text: str | None = None) -> tuple[list[Followup], list[str]]:
    """Parse all ```followup blocks from OPEN_FOLLOWUPS.md text.

    Args:
        text: The markdown text to parse. If None, reads from the canonical file.

    Returns:
        (followups, errors) where followups is the list of successfully parsed
        Followup objects and errors is a list of parse error strings.
    """
    if text is None:
        text = _read_followups_md()

    followups: list[Followup] = []
    errors: list[str] = []

    for match in _FOLLOWUP_BLOCK_RE.finditer(text):
        block_text = match.group(0)
        body = match.group("body").strip()
        line_start = text[: match.start()].count("\n") + 1
        line_end = text[: match.end()].count("\n") + 1

        result = _block_to_followup(block_text, body, line_start, line_end)
        if isinstance(result, str):
            errors.append(result)
        else:
            followups.append(result)

    return followups, errors


def _read_followups_md() -> str:
    """Read OPEN_FOLLOWUPS.md from the context mirror."""
    if _FOLLOWUPS_MD.exists():
        return _FOLLOWUPS_MD.read_text(encoding="utf-8")
    return ""


# ── State sidecar ────────────────────────────────────────────────────────────


def _ensure_followups_dir() -> None:
    """Ensure the followups/ directory exists."""
    _FOLLOWUPS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, FollowupState]:
    """Load the state sidecar from disk."""
    if not _STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return {k: FollowupState(**v) for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_state(state: dict[str, FollowupState]) -> None:
    """Atomically save the state sidecar to disk (tmp + os.replace)."""
    _ensure_followups_dir()
    serializable = {k: asdict(v) for k, v in state.items()}
    tmp_path = _STATE_PATH.with_name(f"state.json.{os.getpid()}.tmp")
    tmp_path.write_text(
        json.dumps(serializable, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp_path, _STATE_PATH)


def upsert_state(followup_id: str, **kwargs) -> FollowupState:
    """Update or insert a follow-up's state entry. Returns the updated state.

    Keyword args override fields on the existing state. Common fields:
        last_checked, next_check, attempts, last_pinged, status
    """
    state = load_state()
    current = state.get(followup_id, FollowupState())
    for key, value in kwargs.items():
        if hasattr(current, key):
            setattr(current, key, value)
    state[followup_id] = current
    save_state(state)
    return current


# ── Query operations ─────────────────────────────────────────────────────────


def list_open() -> list[tuple[Followup, FollowupState]]:
    """Return all open follow-ups with their state.

    Returns list of (followup, state) tuples. Follow-ups are parsed fresh
    from the .md file each call; state is loaded from the sidecar.
    """
    followups, _ = parse_followups()
    state = load_state()
    result: list[tuple[Followup, FollowupState]] = []
    for f in followups:
        if f.status != "open":
            continue
        s = state.get(f.id, FollowupState())
        result.append((f, s))
    return result


def get(followup_id: str) -> tuple[Followup | None, FollowupState | None]:
    """Get a single follow-up by ID.

    Returns (followup, state) or (None, None) if not found.
    """
    followups, _ = parse_followups()
    state = load_state()
    for f in followups:
        if f.id == followup_id:
            s = state.get(followup_id, FollowupState())
            return f, s
    return None, None


# ── Status mutation (edits the .md block + sidecar) ──────────────────────────


def _replace_block_in_text(text: str, followup: Followup, new_status: str) -> str:
    """Replace a follow-up block's status line and move it to the appropriate
    section (Resolved or Aborted) in the markdown.

    This is a best-effort in-memory replacement. The caller is responsible
    for writing the result back to disk.
    """
    # 1. Update the status line inside the block
    old_block = followup._raw_block
    new_block = re.sub(
        r"^status:\s*open$",
        f"status: {new_status}",
        old_block,
        flags=re.MULTILINE,
    )

    # 2. Replace the old block with the updated block in the full text
    text = text.replace(old_block, new_block, 1)

    # 3. Move the block to the appropriate section
    # Find the target section header
    if new_status == "resolved":
        section_header = "## Recently shipped"
    elif new_status == "aborted":
        section_header = "## Closed without shipping"
    else:
        # For other statuses, just update in place
        return text

    # Remove the block from its current position
    text = text.replace(new_block, "", 1)

    # Find the section header and insert after it
    section_pos = text.find(section_header)
    if section_pos >= 0:
        # Find the end of the section header line
        eol = text.find("\n", section_pos)
        insert_pos = eol + 1 if eol >= 0 else len(text)
        # Insert with a blank line separator
        text = text[:insert_pos] + "\n\n" + new_block.strip() + "\n" + text[insert_pos:]
    else:
        # Section not found — append to end
        text = text.rstrip() + "\n\n" + new_block.strip() + "\n"

    return text


def set_status(followup_id: str, new_status: str) -> tuple[bool, str]:
    """Set the status of a follow-up.

    Updates both the .md block (moves to Resolved/Aborted section) and the
    sidecar state. Returns (success, message).

    Valid statuses: 'open', 'resolved', 'aborted'.
    """
    if new_status not in ("open", "resolved", "aborted"):
        return (
            False,
            f"Invalid status: '{new_status}'. Must be 'open', 'resolved', or 'aborted'.",
        )

    followup, state = get(followup_id)
    if followup is None:
        return False, f"Follow-up '{followup_id}' not found."

    # Read the current .md text
    text = _read_followups_md()
    if not text:
        return False, "Cannot read OPEN_FOLLOWUPS.md"

    # Update the block in the text
    updated_text = _replace_block_in_text(text, followup, new_status)

    # Write back atomically
    tmp_path = _FOLLOWUPS_MD.with_name(f"OPEN_FOLLOWUPS.md.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(updated_text, encoding="utf-8")
        os.replace(tmp_path, _FOLLOWUPS_MD)
    except OSError as e:
        return False, f"Failed to write OPEN_FOLLOWUPS.md: {e}"

    # Update sidecar state
    upsert_state(followup_id, status=new_status)

    return True, f"Follow-up '{followup_id}' status set to '{new_status}'."


# ── Convenience ──────────────────────────────────────────────────────────────


def format_followup(f: Followup, s: FollowupState | None = None) -> str:
    """Format a follow-up as a human-readable string."""
    lines = [
        f"**{f.title}** (`{f.id}`)",
        f"  Thread: {f.thread_id} | Status: {f.status}",
        f"  Created: {f.created_at}",
    ]
    if f.condition:
        lines.append(f"  Condition: {f.condition.get('kind', '?')}")
    if f.schedule:
        lines.append(f"  Schedule: {f.schedule.get('check', '?')}")
        if f.schedule.get("escalate_after_days"):
            lines.append(f"  Escalate after: {f.schedule['escalate_after_days']} days")
    if s:
        lines.append(f"  Attempts: {s.attempts}")
        if s.last_checked:
            lines.append(f"  Last checked: {s.last_checked}")
        if s.next_check:
            lines.append(f"  Next check: {s.next_check}")
        if s.last_pinged:
            lines.append(f"  Last pinged: {s.last_pinged}")
    return "\n".join(lines)


def now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
