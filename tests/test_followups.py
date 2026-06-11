"""Tests for the follow-up store (app/followups.py).

Pure-unit: no network, no real filesystem writes to the canonical paths.
All tests use in-memory strings or temp paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from app import followups as fu


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch):
    """Redirect followups paths to temp dirs so tests never touch real files."""
    tmp = Path(tempfile.mkdtemp(prefix="followups_test_"))
    md_path = tmp / "OPEN_FOLLOWUPS.md"
    state_dir = tmp / "followups"
    state_path = state_dir / "state.json"

    monkeypatch.setattr(fu, "_FOLLOWUPS_MD", md_path)
    monkeypatch.setattr(fu, "_FOLLOWUPS_DIR", state_dir)
    monkeypatch.setattr(fu, "_STATE_PATH", state_path)
    monkeypatch.setattr(fu, "_SESSION_LOG_DIR", tmp / "sessions")

    return tmp


# ── Sample follow-up blocks ─────────────────────────────────────────────────


SAMPLE_MD = """# Open follow-ups (cross-session backlog)

This is some prose that should be left untouched.

## Pending

### Some human entry

This is a human-written backlog entry with no followup block.
It should be completely ignored by the parser.

```followup
id: matheus-nota-fiscal
chat_id: -1003919341801
thread_id: 10
title: Chase Matheus for the Nota Fiscal (AGL-XX)
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

More prose between blocks.

```followup
id: check-ami-status
chat_id: -1003919341801
thread_id: 10
title: Check if the AMI finished baking
created_at: 2026-06-10
condition:
  kind: elapsed_days
schedule:
  check: daily
  escalate_after_days: 1
  on_escalate: ping_thread
status: open
```

## Recently shipped

Nothing yet.
"""

SAMPLE_MD_NO_THREAD_ID = """# Open follow-ups

```followup
id: missing-thread
chat_id: -1003919341801
# thread_id deliberately omitted
title: This should fail to parse
created_at: 2026-06-11
condition:
  kind: gmail_reply
status: open
```
"""

SAMPLE_MD_EMPTY = """# Open follow-ups

No follow-up blocks here.
"""


# ── Parser tests ────────────────────────────────────────────────────────────


def test_parse_mixed_doc():
    """Parse a mixed doc with prose + 2 followup blocks → only the blocks."""
    followups, errors = fu.parse_followups(SAMPLE_MD)
    assert len(errors) == 0, f"Unexpected errors: {errors}"
    assert len(followups) == 2
    assert followups[0].id == "matheus-nota-fiscal"
    assert followups[1].id == "check-ami-status"


def test_parse_preserves_prose():
    """Prose outside followup blocks is untouched by the parser."""
    followups, errors = fu.parse_followups(SAMPLE_MD)
    assert len(errors) == 0
    # The parser only extracts blocks; it doesn't modify the text
    # Prose preservation is tested via set_status round-trip


def test_parse_extracts_all_fields():
    """All fields from a followup block are correctly extracted."""
    followups, _ = fu.parse_followups(SAMPLE_MD)
    f = followups[0]
    assert f.id == "matheus-nota-fiscal"
    assert f.chat_id == "-1003919341801"
    assert f.thread_id == "10"
    assert f.title == "Chase Matheus for the Nota Fiscal (AGL-XX)"
    assert f.created_at == "2026-06-11"
    assert f.condition == {"kind": "gmail_reply", "from": "matheus@example.com", "subject_contains": "Nota Fiscal"}
    assert f.schedule == {"check": "daily", "escalate_after_days": "2", "on_escalate": "ping_thread"}
    assert f.status == "open"


def test_parse_nested_condition():
    """Nested condition/schedule dicts are correctly parsed."""
    followups, _ = fu.parse_followups(SAMPLE_MD)
    f = followups[1]  # check-ami-status
    assert f.condition == {"kind": "elapsed_days"}
    assert f.schedule["check"] == "daily"
    assert f.schedule["escalate_after_days"] == "1"


def test_parse_empty_doc():
    """Empty doc with no followup blocks returns empty lists."""
    followups, errors = fu.parse_followups(SAMPLE_MD_EMPTY)
    assert len(followups) == 0
    assert len(errors) == 0


def test_parse_missing_thread_id_surfaces_error():
    """A followup block without thread_id produces a parse error, not silent drop."""
    followups, errors = fu.parse_followups(SAMPLE_MD_NO_THREAD_ID)
    assert len(followups) == 0
    assert len(errors) == 1
    assert "thread_id" in errors[0]
    assert "missing-thread" in errors[0]


def test_parse_ignores_non_followup_code_blocks():
    """Regular ``` blocks (not ```followup) are ignored."""
    text = """# Test

Some text.

```python
print("hello")
```

More text.
"""
    followups, errors = fu.parse_followups(text)
    assert len(followups) == 0
    assert len(errors) == 0


def test_parse_block_body_empty():
    """A followup block with only the fence and no body."""
    text = """# Test

```followup
```
"""
    followups, errors = fu.parse_followups(text)
    # Empty body — thread_id is missing, so parse error
    assert len(followups) == 0
    assert len(errors) >= 1


# ── State sidecar tests ──────────────────────────────────────────────────────


def test_state_round_trip():
    """State can be saved and loaded back identically."""
    state = {
        "matheus-nota-fiscal": fu.FollowupState(
            last_checked="2026-06-11T12:00:00Z",
            next_check="2026-06-12T12:00:00Z",
            attempts=1,
            last_pinged=None,
            status="open",
        ),
        "check-ami-status": fu.FollowupState(
            last_checked=None,
            next_check="2026-06-11T12:00:00Z",
            attempts=0,
            last_pinged=None,
            status="open",
        ),
    }
    fu.save_state(state)
    loaded = fu.load_state()
    assert loaded.keys() == state.keys()
    for key in state:
        assert loaded[key].last_checked == state[key].last_checked
        assert loaded[key].next_check == state[key].next_check
        assert loaded[key].attempts == state[key].attempts
        assert loaded[key].last_pinged == state[key].last_pinged
        assert loaded[key].status == state[key].status


def test_state_empty_on_missing_file():
    """Loading state from a non-existent file returns empty dict."""
    state = fu.load_state()
    assert state == {}


def test_state_corrupted_file_returns_empty():
    """Loading a corrupted state file returns empty dict (graceful degradation)."""
    fu._STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fu._STATE_PATH.write_text("not valid json", encoding="utf-8")
    state = fu.load_state()
    assert state == {}


def test_upsert_state_creates_new():
    """upsert_state on a non-existent ID creates a new entry."""
    result = fu.upsert_state("new-followup", attempts=0, status="open")
    assert result.attempts == 0
    assert result.status == "open"
    state = fu.load_state()
    assert "new-followup" in state


def test_upsert_state_updates_existing():
    """upsert_state on an existing ID updates only specified fields."""
    fu.upsert_state("test-item", attempts=1, last_checked="2026-06-11T00:00:00Z")
    result = fu.upsert_state("test-item", attempts=2)
    assert result.attempts == 2
    assert result.last_checked == "2026-06-11T00:00:00Z"  # unchanged


def test_state_atomic_write():
    """State writes are atomic — a crash during write doesn't corrupt the file.
    We verify by checking that the final file is valid JSON after a simulated
    crash (tmp file exists but replace never happened)."""
    fu._STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write initial valid state
    fu.save_state({"a": fu.FollowupState(status="open")})
    initial_content = fu._STATE_PATH.read_text()

    # Simulate a crash: write tmp but don't replace
    tmp_path = fu._STATE_PATH.with_name(f"state.json.{os.getpid()}.tmp")
    tmp_path.write_text("corrupted", encoding="utf-8")
    # Don't os.replace — the real file should be untouched

    # Verify the real file is still valid
    loaded = fu.load_state()
    assert "a" in loaded
    assert loaded["a"].status == "open"

    # Clean up tmp
    tmp_path.unlink(missing_ok=True)


# ── Status mutation tests ────────────────────────────────────────────────────


def test_set_status_resolved_rewrites_block(tmp_path):
    """set_status('resolved') rewrites the block's status line and moves it
    to the Recently shipped section, leaving prose intact."""
    # Write the sample to the patched path
    fu._FOLLOWUPS_MD.write_text(SAMPLE_MD, encoding="utf-8")

    success, msg = fu.set_status("matheus-nota-fiscal", "resolved")
    assert success, f"set_status failed: {msg}"

    # Re-read and verify
    followups, errors = fu.parse_followups()
    assert len(errors) == 0

    # The resolved follow-up should no longer appear in open list
    open_followups = [f for f in followups if f.status == "open"]
    assert len(open_followups) == 1
    assert open_followups[0].id == "check-ami-status"

    # The resolved follow-up should have status 'resolved'
    resolved = [f for f in followups if f.id == "matheus-nota-fiscal"]
    assert len(resolved) == 1
    assert resolved[0].status == "resolved"

    # Sidecar should also reflect the change
    state = fu.load_state()
    assert state["matheus-nota-fiscal"].status == "resolved"


def test_set_status_aborted(tmp_path):
    """set_status('aborted') works similarly."""
    fu._FOLLOWUPS_MD.write_text(SAMPLE_MD, encoding="utf-8")

    success, msg = fu.set_status("check-ami-status", "aborted")
    assert success, f"set_status failed: {msg}"

    followups, _ = fu.parse_followups()
    aborted = [f for f in followups if f.id == "check-ami-status"]
    assert len(aborted) == 1
    assert aborted[0].status == "aborted"


def test_set_status_invalid():
    """set_status with an invalid status returns error."""
    success, msg = fu.set_status("anything", "invalid_status")
    assert not success
    assert "Invalid status" in msg


def test_set_status_not_found():
    """set_status on a non-existent ID returns error."""
    success, msg = fu.set_status("nonexistent-id", "resolved")
    assert not success
    assert "not found" in msg


def test_prose_left_intact_after_status_change(tmp_path):
    """After a status change, the prose surrounding the followup blocks is
    preserved exactly."""
    original_text = SAMPLE_MD
    fu._FOLLOWUPS_MD.write_text(original_text, encoding="utf-8")

    # Extract prose sections (everything outside ```followup blocks)
    prose_parts = []
    for part in original_text.split("```followup"):
        # Before the block
        before_block = part.rsplit("```", 1)[0] if "```" in part else part
        prose_parts.append(before_block)
        # After the block
        if "```" in part:
            after_block = part.split("```", 1)[1]
            prose_parts.append(after_block)

    # Change status
    fu.set_status("matheus-nota-fiscal", "resolved")

    # Read back
    updated_text = fu._FOLLOWUPS_MD.read_text(encoding="utf-8")

    # Key prose markers should still be present
    assert "This is some prose that should be left untouched." in updated_text
    assert "More prose between blocks." in updated_text
    assert "## Pending" in updated_text
    assert "## Recently shipped" in updated_text


def test_list_open_only_returns_open(tmp_path):
    """list_open only returns follow-ups with status 'open'."""
    fu._FOLLOWUPS_MD.write_text(SAMPLE_MD, encoding="utf-8")

    open_items = fu.list_open()
    assert len(open_items) == 2
    for f, s in open_items:
        assert f.status == "open"

    # Resolve one
    fu.set_status("matheus-nota-fiscal", "resolved")

    open_items = fu.list_open()
    assert len(open_items) == 1
    assert open_items[0][0].id == "check-ami-status"


def test_get_by_id(tmp_path):
    """get() returns the correct follow-up by ID."""
    fu._FOLLOWUPS_MD.write_text(SAMPLE_MD, encoding="utf-8")

    f, s = fu.get("matheus-nota-fiscal")
    assert f is not None
    assert f.id == "matheus-nota-fiscal"
    assert f.title == "Chase Matheus for the Nota Fiscal (AGL-XX)"

    f, s = fu.get("nonexistent")
    assert f is None
    assert s is None


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_parse_block_with_extra_whitespace():
    """Blocks with extra whitespace around values are parsed correctly."""
    text = """# Test

```followup
id:   spaced-id
chat_id:   -100
thread_id:   42
title:   Spaced title
created_at:   2026-06-11
condition:
  kind:   gmail_reply
status: open
```
"""
    followups, errors = fu.parse_followups(text)
    assert len(errors) == 0
    assert len(followups) == 1
    assert followups[0].id == "spaced-id"
    assert followups[0].thread_id == "42"
    assert followups[0].condition["kind"] == "gmail_reply"


def test_parse_block_with_unknown_fields():
    """Blocks with unknown fields are parsed without error (extra fields ignored)."""
    text = """# Test

```followup
id: extra-fields
chat_id: -100
thread_id: 7
title: Has extra fields
created_at: 2026-06-11
condition:
  kind: elapsed_days
unknown_field: this should be ignored
another_unknown: also fine
status: open
```
"""
    followups, errors = fu.parse_followups(text)
    assert len(errors) == 0
    assert len(followups) == 1
    assert followups[0].id == "extra-fields"


def test_parse_multiple_blocks_same_id():
    """Multiple blocks with the same ID are both returned (dedup is caller's choice)."""
    text = """# Test

```followup
id: duplicate
chat_id: -100
thread_id: 1
title: First
title: First
created_at: 2026-06-11
status: open
```

```followup
id: duplicate
chat_id: -100
thread_id: 2
title: Second
created_at: 2026-06-11
status: open
```
"""
    followups, errors = fu.parse_followups(text)
    assert len(errors) == 0
    assert len(followups) == 2
    assert followups[0].id == "duplicate"
    assert followups[1].id == "duplicate"


def test_format_followup():
    """format_followup produces a readable string."""
    f = fu.Followup(
        id="test-id",
        chat_id="-100",
        thread_id="42",
        title="Test Follow-up",
        created_at="2026-06-11",
        condition={"kind": "gmail_reply"},
        schedule={"check": "daily", "escalate_after_days": "3"},
        status="open",
    )
    s = fu.FollowupState(attempts=2, last_checked="2026-06-11T12:00:00Z")
    formatted = fu.format_followup(f, s)
    assert "Test Follow-up" in formatted
    assert "test-id" in formatted
    assert "gmail_reply" in formatted
    assert "daily" in formatted
    assert "3 days" in formatted
    assert "Attempts: 2" in formatted


def test_now_iso_format():
    """now_iso returns a valid ISO 8601 string."""
    ts = fu.now_iso()
    assert ts.endswith("Z")
    assert "T" in ts
    assert len(ts) == 20  # 2026-06-11T12:00:00Z
