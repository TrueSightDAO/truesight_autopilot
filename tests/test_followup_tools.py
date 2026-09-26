"""
Tests for app/tools/followup_tools.py — follow-up tools.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tg_ctx() -> MagicMock:
    """A mock Telegram session context."""
    ctx = MagicMock()
    ctx.session_id = "tg:-1003919341801:2622"
    return ctx


@pytest.fixture
def non_tg_ctx() -> MagicMock:
    """A mock non-Telegram session context."""
    ctx = MagicMock()
    ctx.session_id = "web:abc123"
    return ctx


@pytest.fixture
def sample_open_followups() -> list[dict]:
    """Sample parsed follow-ups for testing list/close."""
    return [
        {
            "id": "test-followup-1",
            "chat_id": "-1003919341801",
            "thread_id": 2622,
            "title": "Test follow-up 1",
            "created_at": "2026-06-11",
            "condition": {"kind": "elapsed_days"},
            "schedule": {
                "check": "daily",
                "escalate_after_days": 1,
                "on_escalate": "ping_thread",
            },
            "status": "open",
        },
        {
            "id": "test-followup-2",
            "chat_id": "-1003919341801",
            "thread_id": 10,
            "title": "Test follow-up 2 (different thread)",
            "created_at": "2026-06-11",
            "condition": {"kind": "gmail_reply", "from": "test@example.com"},
            "schedule": {
                "check": "daily",
                "escalate_after_days": 2,
                "on_escalate": "ping_thread",
            },
            "status": "open",
        },
        {
            "id": "test-followup-3",
            "chat_id": "-1003919341801",
            "thread_id": 2622,
            "title": "Test follow-up 3 (same thread)",
            "created_at": "2026-06-11",
            "condition": {"kind": "elapsed_days"},
            "schedule": {
                "check": "weekly",
                "escalate_after_days": 7,
                "on_escalate": "ping_thread",
            },
            "status": "open",
        },
    ]


# ── add_followup tests ───────────────────────────────────────────────────


class TestAddFollowup:
    def test_refuses_non_telegram(self, non_tg_ctx: MagicMock):
        """add_followup refuses non-Telegram sessions."""
        from app.tools.followup_tools import add_followup

        result = json.loads(add_followup(non_tg_ctx, "test-id", "Test"))
        assert result["status"] == "error"
        assert "Telegram session" in result["message"]

    def test_requires_thread_id(self, tg_ctx: MagicMock):
        """add_followup requires thread_id."""
        from app.tools.followup_tools import add_followup

        # Patch _derive_thread_id to return None
        with patch("app.tools.followup_tools._derive_thread_id", return_value=None):
            result = json.loads(add_followup(tg_ctx, "test-id", "Test"))
            assert result["status"] == "error"
            assert "thread_id is required" in result["message"]

    def test_creates_followup(self, tg_ctx: MagicMock):
        """add_followup creates a follow-up successfully."""
        from app.tools.followup_tools import add_followup

        with (
            patch(
                "app.tools.followup_tools._read_md",
                return_value="# Test\n\n## Pending\n\n",
            ),
            patch("app.tools.followup_tools._write_md"),
            patch("app.tools.followup_tools.upsert_state"),
        ):
            result = json.loads(
                add_followup(
                    tg_ctx,
                    "test-id",
                    "Test follow-up",
                    condition_kind="elapsed_days",
                    escalate_after_days=3,
                    check="weekly",
                )
            )
            assert result["status"] == "ok"
            assert result["followup"]["id"] == "test-id"
            assert result["followup"]["title"] == "Test follow-up"
            assert result["followup"]["escalate_after_days"] == 3

    def test_creates_gmail_reply_followup(self, tg_ctx: MagicMock):
        """add_followup creates a gmail_reply follow-up with extra fields."""
        from app.tools.followup_tools import add_followup

        with (
            patch(
                "app.tools.followup_tools._read_md",
                return_value="# Test\n\n## Pending\n\n",
            ),
            patch("app.tools.followup_tools._write_md"),
            patch("app.tools.followup_tools.upsert_state"),
        ):
            result = json.loads(
                add_followup(
                    tg_ctx,
                    "email-test",
                    "Wait for email reply",
                    condition_kind="gmail_reply",
                    escalate_after_days=2,
                    check="daily",
                    from_="partner@example.com",
                    subject_contains="Quote",
                )
            )
            assert result["status"] == "ok"
            assert result["followup"]["condition"] == "gmail_reply"

    def test_derives_thread_from_session(self, tg_ctx: MagicMock):
        """add_followup derives thread_id from session when not provided."""
        from app.tools.followup_tools import add_followup

        with (
            patch(
                "app.tools.followup_tools._read_md",
                return_value="# Test\n\n## Pending\n\n",
            ),
            patch("app.tools.followup_tools._write_md"),
            patch("app.tools.followup_tools.upsert_state"),
        ):
            result = json.loads(
                add_followup(
                    tg_ctx,
                    "auto-thread",
                    "Auto-derived thread",
                )
            )
            assert result["status"] == "ok"


# ── list_followups tests ─────────────────────────────────────────────────


class TestListFollowups:
    def test_empty_list(self, tg_ctx: MagicMock):
        """list_followups returns empty when no follow-ups exist."""
        from app.tools.followup_tools import list_followups

        with patch("app.tools.followup_tools.list_open", return_value=[]):
            result = json.loads(list_followups(tg_ctx))
            assert result["status"] == "ok"
            assert result["followups"] == []

    def test_lists_all_open(self, tg_ctx: MagicMock, sample_open_followups: list[dict]):
        """list_followups returns all open follow-ups."""
        from app.tools.followup_tools import list_followups

        with (
            patch(
                "app.tools.followup_tools.list_open", return_value=sample_open_followups
            ),
            patch("app.tools.followup_tools.get_state", return_value=None),
        ):
            result = json.loads(list_followups(tg_ctx))
            assert result["status"] == "ok"
            assert result["count"] == 3

    def test_filters_by_this_thread(
        self, tg_ctx: MagicMock, sample_open_followups: list[dict]
    ):
        """list_followups with this_thread=True filters to current thread."""
        from app.tools.followup_tools import list_followups

        with (
            patch(
                "app.tools.followup_tools.list_open", return_value=sample_open_followups
            ),
            patch("app.tools.followup_tools.get_state", return_value=None),
        ):
            result = json.loads(list_followups(tg_ctx, this_thread=True))
            assert result["status"] == "ok"
            assert result["count"] == 2
            ids = [f["id"] for f in result["followups"]]
            assert "test-followup-1" in ids
            assert "test-followup-3" in ids
            assert "test-followup-2" not in ids

    def test_enriches_with_state(
        self, tg_ctx: MagicMock, sample_open_followups: list[dict]
    ):
        """list_followups enriches entries with sidecar state."""
        from app.tools.followup_tools import list_followups

        mock_state = {
            "attempts": 5,
            "last_checked": "2026-06-11T12:00:00+00:00",
            "next_check": "2026-06-12T12:00:00+00:00",
        }

        with (
            patch(
                "app.tools.followup_tools.list_open", return_value=sample_open_followups
            ),
            patch("app.tools.followup_tools.get_state", return_value=mock_state),
        ):
            result = json.loads(list_followups(tg_ctx))
            entry = result["followups"][0]
            assert entry["attempts"] == 5
            assert entry["last_checked"] == "2026-06-11T12:00:00+00:00"


# ── close_followup tests ─────────────────────────────────────────────────


class TestCloseFollowup:
    def test_close_resolved(self, tg_ctx: MagicMock):
        """close_followup with status=resolved works."""
        from app.tools.followup_tools import close_followup

        with patch("app.tools.followup_tools.set_status", return_value=True):
            result = json.loads(close_followup(tg_ctx, "test-id", "resolved"))
            assert result["status"] == "ok"
            assert "resolved" in result["message"]

    def test_close_aborted(self, tg_ctx: MagicMock):
        """close_followup with status=aborted works."""
        from app.tools.followup_tools import close_followup

        with patch("app.tools.followup_tools.set_status", return_value=True):
            result = json.loads(close_followup(tg_ctx, "test-id", "aborted"))
            assert result["status"] == "ok"
            assert "aborted" in result["message"]

    def test_close_nonexistent(self, tg_ctx: MagicMock):
        """close_followup returns error for unknown id."""
        from app.tools.followup_tools import close_followup

        with patch("app.tools.followup_tools.set_status", return_value=False):
            result = json.loads(close_followup(tg_ctx, "nonexistent", "resolved"))
            assert result["status"] == "error"
            assert "not found" in result["message"]

    def test_close_invalid_status(self, tg_ctx: MagicMock):
        """close_followup returns error for invalid status."""
        from app.tools.followup_tools import close_followup

        result = json.loads(close_followup(tg_ctx, "test-id", "invalid"))
        assert result["status"] == "error"
        assert "Invalid status" in result["message"]


# ── helper tests ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_is_telegram_session_true(self, tg_ctx: MagicMock):
        """_is_telegram_session returns True for tg: sessions."""
        from app.tools.followup_tools import _is_telegram_session

        assert _is_telegram_session(tg_ctx) is True

    def test_is_telegram_session_false(self, non_tg_ctx: MagicMock):
        """_is_telegram_session returns False for non-tg sessions."""
        from app.tools.followup_tools import _is_telegram_session

        assert _is_telegram_session(non_tg_ctx) is False

    def test_derive_thread_id(self, tg_ctx: MagicMock):
        """_derive_thread_id extracts thread_id from tg: session."""
        from app.tools.followup_tools import _derive_thread_id

        assert _derive_thread_id(tg_ctx) == "2622"

    def test_derive_chat_id(self, tg_ctx: MagicMock):
        """_derive_chat_id extracts chat_id from tg: session."""
        from app.tools.followup_tools import _derive_chat_id

        assert _derive_chat_id(tg_ctx) == "-1003919341801"

    def test_derive_thread_id_non_tg(self, non_tg_ctx: MagicMock):
        """_derive_thread_id returns None for non-tg sessions."""
        from app.tools.followup_tools import _derive_thread_id

        assert _derive_thread_id(non_tg_ctx) is None
