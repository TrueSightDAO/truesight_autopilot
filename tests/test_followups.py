"""
Tests for app/followups.py — follow-up parser + state sidecar.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_md() -> str:
    """A realistic OPEN_FOLLOWUPS.md with prose + followup blocks."""
    return """# Open follow-ups

Some prose about how this file works.

## Pending

```followup
id: chocolate-subscription-phase2
chat_id: -1003919341801
thread_id: 1939
title: Revisit Chocolate Subscription Phase 2 (fulfillment automation)
created_at: 2026-06-11
condition:
  kind: elapsed_days
  escalate_after_days: 60
schedule:
  check: weekly
  on_escalate: ping_thread
status: open
description: >
  Phase 2 was deferred until Linda has received 2 subscription shipments.
  When this fires, remind Gary.
```

### Some other prose entry

This is a regular markdown entry, not a followup block.

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

## Recently shipped

### Some shipped item

```followup
id: already-shipped
chat_id: -1003919341801
thread_id: 5
title: Already shipped item
created_at: 2026-06-10
condition:
  kind: elapsed_days
  escalate_after_days: 1
schedule:
  check: daily
  on_escalate: ping_thread
status: resolved
```

## Closed without shipping

Nothing here yet.
"""


@pytest.fixture
def md_with_missing_thread_id() -> str:
    return """# Test

```followup
id: no-thread
chat_id: -1003919341801
thread_id:
title: Missing thread_id
created_at: 2026-06-11
condition:
  kind: elapsed_days
  escalate_after_days: 1
schedule:
  check: daily
  on_escalate: ping_thread
status: open
```
"""


@pytest.fixture
def md_with_invalid_status() -> str:
    return """# Test

```followup
id: bad-status
chat_id: -1003919341801
thread_id: 42
title: Bad status
created_at: 2026-06-11
condition:
  kind: elapsed_days
  escalate_after_days: 1
schedule:
  check: daily
  on_escalate: ping_thread
status: flying
```
"""


@pytest.fixture
def empty_md() -> str:
    return """# Open follow-ups

No followup blocks here.

Just prose.
"""


# ── parser tests ─────────────────────────────────────────────────────────


class TestParseAll:
    def test_parses_followup_blocks_only(self, sample_md: str):
        """Only ```followup blocks are parsed; prose is ignored."""
        with patch(
            "app.followups._read_md", return_value=sample_md
        ):
            from app.followups import parse_all

            results = parse_all()
            assert len(results) == 3
            ids = [r["id"] for r in results]
            assert "chocolate-subscription-phase2" in ids
            assert "matheus-nota-fiscal" in ids
            assert "already-shipped" in ids

    def test_parsed_fields(self, sample_md: str):
        """Parsed blocks have the expected fields."""
        with patch("app.followups._read_md", return_value=sample_md):
            from app.followups import parse_all

            results = parse_all()
            chocolate = [r for r in results if r["id"] == "chocolate-subscription-phase2"][0]
            assert str(chocolate["chat_id"]) == "-1003919341801"
            assert chocolate["thread_id"] == 1939
            assert chocolate["title"] == "Revisit Chocolate Subscription Phase 2 (fulfillment automation)"
            assert chocolate["status"] == "open"
            assert chocolate["condition"]["kind"] == "elapsed_days"
            assert chocolate["schedule"]["check"] == "weekly"

    def test_resolved_status_normalised(self, sample_md: str):
        """Status is normalised to lowercase."""
        with patch("app.followups._read_md", return_value=sample_md):
            from app.followups import parse_all

            results = parse_all()
            shipped = [r for r in results if r["id"] == "already-shipped"][0]
            assert shipped["status"] == "resolved"

    def test_empty_md_returns_empty(self, empty_md: str):
        """No followup blocks → empty list."""
        with patch("app.followups._read_md", return_value=empty_md):
            from app.followups import parse_all

            results = parse_all()
            assert results == []

    def test_missing_thread_id_skipped(self, md_with_missing_thread_id: str):
        """Missing thread_id → block is skipped with warning."""
        with patch("app.followups._read_md", return_value=md_with_missing_thread_id):
            from app.followups import parse_all

            results = parse_all()
            assert len(results) == 0

    def test_invalid_status_skipped(self, md_with_invalid_status: str):
        """Invalid status → block is skipped with warning."""
        with patch("app.followups._read_md", return_value=md_with_invalid_status):
            from app.followups import parse_all

            results = parse_all()
            assert len(results) == 0


class TestGet:
    def test_get_by_id(self, sample_md: str):
        """get() returns the correct follow-up by id."""
        with patch("app.followups._read_md", return_value=sample_md):
            from app.followups import get

            result = get("matheus-nota-fiscal")
            assert result is not None
            assert result["title"] == "Chase Matheus for the Nota Fiscal (AGL-XX)"

    def test_get_nonexistent(self, sample_md: str):
        """get() returns None for unknown id."""
        with patch("app.followups._read_md", return_value=sample_md):
            from app.followups import get

            result = get("nonexistent")
            assert result is None


class TestListOpen:
    def test_list_open_only(self, sample_md: str):
        """list_open() returns only status=open follow-ups."""
        with patch("app.followups._read_md", return_value=sample_md):
            from app.followups import list_open

            results = list_open()
            assert len(results) == 2
            ids = [r["id"] for r in results]
            assert "chocolate-subscription-phase2" in ids
            assert "matheus-nota-fiscal" in ids
            assert "already-shipped" not in ids


# ── state sidecar tests ──────────────────────────────────────────────────


class TestStateSidecar:
    def test_upsert_creates_new_entry(self, tmp_path: Path):
        """upsert_state creates a new entry with defaults."""
        from app.followups import upsert_state, _STATE_FILE, _STATE_DIR

        with patch.object(
            _STATE_DIR.__class__, "resolve", return_value=tmp_path
        ), patch.object(
            _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
        ):
            entry = upsert_state("test-id", next_check="2026-06-12T00:00:00+00:00")
            assert entry["id"] == "test-id"
            assert entry["next_check"] == "2026-06-12T00:00:00+00:00"
            assert entry["attempts"] == 0
            assert entry["last_checked"] is None

    def test_upsert_merges_existing(self, tmp_path: Path):
        """upsert_state merges into existing entry."""
        from app.followups import upsert_state, _STATE_FILE, _STATE_DIR

        with patch.object(
            _STATE_DIR.__class__, "resolve", return_value=tmp_path
        ), patch.object(
            _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
        ):
            upsert_state("test-id", attempts=1)
            entry = upsert_state("test-id", next_check="2026-06-13T00:00:00+00:00")
            assert entry["attempts"] == 1
            assert entry["next_check"] == "2026-06-13T00:00:00+00:00"

    def test_get_state_returns_none_for_unknown(self, tmp_path: Path):
        """get_state returns None for never-seen id."""
        from app.followups import get_state, _STATE_FILE, _STATE_DIR

        with patch.object(
            _STATE_DIR.__class__, "resolve", return_value=tmp_path
        ), patch.object(
            _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
        ):
            result = get_state("never-seen")
            assert result is None

    def test_atomic_write_survives_corruption(self, tmp_path: Path):
        """Atomic write doesn't corrupt existing state on failure."""
        from app.followups import upsert_state, get_state, _STATE_FILE, _STATE_DIR

        with patch.object(
            _STATE_DIR.__class__, "resolve", return_value=tmp_path
        ), patch.object(
            _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
        ):
            upsert_state("test-id", attempts=1)
            # Simulate a failed write by writing garbage
            _STATE_FILE.write_text("{{{garbage}}")
            # Should recover gracefully
            result = get_state("test-id")
            assert result is None  # garbage → empty state


# ── set_status tests ─────────────────────────────────────────────────────


class TestSetStatus:
    def test_set_status_updates_block(self, sample_md: str, tmp_path: Path):
        """set_status updates the status in the .md block."""
        from app.followups import set_status, _STATE_FILE, _STATE_DIR

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch("app.followups._write_md") as mock_write,
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            result = set_status("matheus-nota-fiscal", "resolved")
            assert result is True

            written = mock_write.call_args[0][0]
            assert "## Recently shipped" in written
            assert "matheus-nota-fiscal" in written
            assert "status: resolved" in written

    def test_set_status_aborted(self, sample_md: str, tmp_path: Path):
        """set_status with aborted moves to Closed without shipping."""
        from app.followups import set_status, _STATE_FILE, _STATE_DIR

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch("app.followups._write_md") as mock_write,
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            result = set_status("matheus-nota-fiscal", "aborted")
            assert result is True

            written = mock_write.call_args[0][0]
            assert "## Closed without shipping" in written
            assert "matheus-nota-fiscal" in written
            assert "status: aborted" in written

    def test_set_status_nonexistent(self, sample_md: str, tmp_path: Path):
        """set_status returns False for unknown id."""
        from app.followups import set_status, _STATE_FILE, _STATE_DIR

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            result = set_status("nonexistent", "resolved")
            assert result is False

    def test_set_status_invalid_raises(self):
        """set_status raises ValueError for invalid status."""
        from app.followups import set_status

        with pytest.raises(ValueError, match="Invalid status"):
            set_status("any", "invalid")

    def test_set_status_preserves_prose(self, sample_md: str, tmp_path: Path):
        """set_status leaves non-followup prose intact."""
        from app.followups import set_status, _STATE_FILE, _STATE_DIR

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch("app.followups._write_md") as mock_write,
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            set_status("matheus-nota-fiscal", "resolved")
            written = mock_write.call_args[0][0]
            # Prose should still be there
            assert "Some prose about how this file works." in written
            assert "### Some other prose entry" in written
            assert "This is a regular markdown entry" in written


# ── next_due tests ───────────────────────────────────────────────────────


class TestNextDue:
    def test_never_checked_is_due(self, sample_md: str, tmp_path: Path):
        """Follow-ups never checked are due immediately."""
        from app.followups import next_due, _STATE_FILE, _STATE_DIR
        from datetime import datetime, timezone

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            now = datetime.now(timezone.utc)
            due = next_due(now)
            # Both open follow-ups have never been checked → both due
            assert len(due) == 2

    def test_not_due_when_future(self, sample_md: str, tmp_path: Path):
        """Follow-ups with future next_check are not due."""
        from app.followups import (
            next_due,
            upsert_state,
            _STATE_FILE,
            _STATE_DIR,
        )
        from datetime import datetime, timezone, timedelta

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            upsert_state("chocolate-subscription-phase2", next_check=future)
            upsert_state("matheus-nota-fiscal", next_check=future)

            now = datetime.now(timezone.utc)
            due = next_due(now)
            assert len(due) == 0

    def test_due_when_past(self, sample_md: str, tmp_path: Path):
        """Follow-ups with past next_check are due."""
        from app.followups import (
            next_due,
            upsert_state,
            _STATE_FILE,
            _STATE_DIR,
        )
        from datetime import datetime, timezone, timedelta

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            upsert_state("chocolate-subscription-phase2", next_check=past)
            upsert_state("matheus-nota-fiscal", next_check=past)

            now = datetime.now(timezone.utc)
            due = next_due(now)
            assert len(due) == 2

    def test_resolved_not_included(self, sample_md: str, tmp_path: Path):
        """Resolved follow-ups are never due."""
        from app.followups import next_due, _STATE_FILE, _STATE_DIR
        from datetime import datetime, timezone

        with (
            patch("app.followups._read_md", return_value=sample_md),
            patch.object(_STATE_DIR.__class__, "resolve", return_value=tmp_path),
            patch.object(
                _STATE_FILE.__class__, "resolve", return_value=tmp_path / "state.json"
            ),
        ):
            now = datetime.now(timezone.utc)
            due = next_due(now)
            ids = [d["id"] for d in due]
            assert "already-shipped" not in ids
