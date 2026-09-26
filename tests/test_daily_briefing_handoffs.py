"""Tests for app.daily_briefing._fetch_handoffs against the real
HANDOFF_MANIFEST.md schema (previously this read the wrong repo path, matched
the wrong table-header line, and used an off-by-one exact-match Status check
against values that don't exist — three independent bugs that made it dead
code; see agentic_ai_context/plans/HANDOFF_REGISTRY_CONSOLIDATION_PLAN.md)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

from app import daily_briefing  # noqa: E402

MANIFEST = """\
# Handoff Manifest

Some intro text.

| Plan file | Handoff title | Handoff date | Status | Telegram topic | message_thread_id | Resume tracker state | Last manifest update |
|-----------|---------------|--------------|--------|-----------------|--------------------|----------------------|----------------------|
| `A_PLAN.md` | Plan A | 2026-07-18 | in progress | [A](x) | 1 | RESUME HERE = PR1 | 2026-07-18 |
| `B_PLAN.md` | Plan B | 2026-07-17 | blocked | [B](x) | 2 | RESUME HERE = PR2 | 2026-07-17 |
| `C_PLAN.md` | Plan C | 2026-07-01 | completed | [C](x) | 3 | done | 2026-07-01 |
| `D_PLAN.md` | Plan D | 2026-07-01 | superseded — already implemented | [D](x) | 4 | n/a | 2026-07-01 |

## Status values

| Status | Meaning |
|--------|---------|
| `in progress` | still going |
"""


def _mock_gh(content: str) -> MagicMock:
    gh = MagicMock()
    gh.read_file.return_value = {"type": "file", "content": content}
    return gh


def test_fetch_handoffs_lists_non_terminal_rows():
    with patch("app.daily_briefing.GitHubClient", return_value=_mock_gh(MANIFEST)):
        result = daily_briefing._fetch_handoffs()
    assert "Plan A" in result
    assert "A_PLAN.md" in result
    assert "Plan B" in result


def test_fetch_handoffs_excludes_terminal_statuses():
    with patch("app.daily_briefing.GitHubClient", return_value=_mock_gh(MANIFEST)):
        result = daily_briefing._fetch_handoffs()
    assert "Plan C" not in result
    assert "Plan D" not in result


def test_fetch_handoffs_does_not_pick_up_the_status_legend_table():
    with patch("app.daily_briefing.GitHubClient", return_value=_mock_gh(MANIFEST)):
        result = daily_briefing._fetch_handoffs()
    assert "still going" not in result


def test_fetch_handoffs_no_open_rows_returns_placeholder():
    only_terminal = MANIFEST.split("## Status values")[0].replace(
        "in progress", "completed"
    ).replace("blocked", "completed")
    with patch("app.daily_briefing.GitHubClient", return_value=_mock_gh(only_terminal)):
        result = daily_briefing._fetch_handoffs()
    assert result == "(no active handoffs)"


def test_fetch_handoffs_missing_file_returns_placeholder():
    gh = MagicMock()
    gh.read_file.return_value = {"type": "not_found"}
    with patch("app.daily_briefing.GitHubClient", return_value=gh):
        result = daily_briefing._fetch_handoffs()
    assert result == "(handoffs unavailable)"


def test_fetch_handoffs_exception_returns_placeholder():
    gh = MagicMock()
    gh.read_file.side_effect = RuntimeError("network down")
    with patch("app.daily_briefing.GitHubClient", return_value=gh):
        result = daily_briefing._fetch_handoffs()
    assert result == "(handoffs unavailable)"
