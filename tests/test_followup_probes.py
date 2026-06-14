"""
Tests for app/followup_probes.py — follow-up probes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def elapsed_followup() -> dict:
    """A follow-up with elapsed_days condition."""
    return {
        "id": "test-elapsed",
        "created_at": "2026-06-10",
        "condition": {"kind": "elapsed_days"},
        "schedule": {
            "check": "daily",
            "escalate_after_days": 2,
            "on_escalate": "ping_thread",
        },
        "status": "open",
    }


@pytest.fixture
def gmail_followup() -> dict:
    """A follow-up with gmail_reply condition."""
    return {
        "id": "test-gmail",
        "created_at": "2026-06-10",
        "condition": {
            "kind": "gmail_reply",
            "from": "partner@example.com",
            "subject_contains": "Quote",
        },
        "schedule": {
            "check": "daily",
            "escalate_after_days": 2,
            "on_escalate": "ping_thread",
        },
        "status": "open",
    }


# ── elapsed_days tests ───────────────────────────────────────────────────


class TestElapsedDays:
    def test_not_yet_elapsed(self, elapsed_followup: dict):
        """Returns struck=False when within the threshold."""
        from app.followup_probes import elapsed_days

        now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(elapsed_followup, now)
        assert result["struck"] is False

    def test_exactly_at_threshold(self, elapsed_followup: dict):
        """Returns struck=True when exactly at the threshold."""
        from app.followup_probes import elapsed_days

        now = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(elapsed_followup, now)
        assert result["struck"] is True

    def test_past_threshold(self, elapsed_followup: dict):
        """Returns struck=True when past the threshold."""
        from app.followup_probes import elapsed_days

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(elapsed_followup, now)
        assert result["struck"] is True

    def test_missing_created_at(self):
        """Returns not-struck when created_at is missing."""
        from app.followup_probes import elapsed_days

        followup = {"id": "test", "condition": {"kind": "elapsed_days"}, "schedule": {}}
        result = elapsed_days(followup)
        assert result["struck"] is False

    def test_invalid_created_at(self):
        """Returns not-struck when created_at is invalid."""
        from app.followup_probes import elapsed_days

        followup = {
            "id": "test",
            "created_at": "not-a-date",
            "condition": {"kind": "elapsed_days"},
            "schedule": {},
        }
        result = elapsed_days(followup)
        assert result["struck"] is False

    def test_never_throws(self):
        """elapsed_days never throws an exception."""
        from app.followup_probes import elapsed_days

        result = elapsed_days({})
        assert "struck" in result
        assert "evidence" in result


# ── gmail_reply tests ────────────────────────────────────────────────────


class TestGmailReply:
    def test_no_sender(self, gmail_followup: dict):
        """Returns not-struck when no 'from' address in condition."""
        from app.followup_probes import gmail_reply

        followup = {
            "id": "test",
            "created_at": "2026-06-10",
            "condition": {"kind": "gmail_reply"},
        }
        result = gmail_reply(followup)
        assert result["struck"] is False
        assert "No 'from' address" in result["evidence"]

    def test_no_gmail_service(self, gmail_followup: dict):
        """Returns not-struck when Gmail service is unavailable."""
        from app.followup_probes import gmail_reply

        with patch("app.followup_probes._build_gmail_service", return_value=None):
            result = gmail_reply(gmail_followup)
            assert result["struck"] is False
            assert "Gmail service not available" in result["evidence"]

    def test_no_messages_found(self, gmail_followup: dict):
        """Returns not-struck when no matching messages exist."""
        from app.followup_probes import gmail_reply

        mock_gmail = MagicMock()
        mock_gmail.users().messages().list().execute.return_value = {"messages": []}

        with patch("app.followup_probes._build_gmail_service", return_value=mock_gmail):
            result = gmail_reply(gmail_followup)
            assert result["struck"] is False
            assert "No messages from" in result["evidence"]

    def test_messages_found(self, gmail_followup: dict):
        """Returns struck=True when matching messages exist."""
        from app.followup_probes import gmail_reply

        mock_gmail = MagicMock()
        mock_gmail.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}, {"id": "msg2"}]
        }
        mock_gmail.users().messages().get().execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Re: Quote for cacao"},
                    {"name": "Date", "value": "Fri, 12 Jun 2026 10:00:00 +0000"},
                ]
            }
        }

        with patch("app.followup_probes._build_gmail_service", return_value=mock_gmail):
            result = gmail_reply(gmail_followup)
            assert result["struck"] is True
            assert "Found 2 message(s)" in result["evidence"]
            assert "Re: Quote for cacao" in result["evidence"]

    def test_gmail_error(self, gmail_followup: dict):
        """Returns not-struck when Gmail API throws an error."""
        from app.followup_probes import gmail_reply

        mock_gmail = MagicMock()
        mock_gmail.users().messages().list().execute.side_effect = Exception(
            "API error"
        )

        with patch("app.followup_probes._build_gmail_service", return_value=mock_gmail):
            result = gmail_reply(gmail_followup)
            assert result["struck"] is False
            assert "Gmail query error" in result["evidence"]


# ── run_probe tests ──────────────────────────────────────────────────────


class TestRunProbe:
    def test_dispatches_elapsed_days(self, elapsed_followup: dict):
        """run_probe dispatches to elapsed_days probe."""
        from app.followup_probes import run_probe

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
        result = run_probe(elapsed_followup, now)
        assert result["struck"] is True

    def test_dispatches_gmail_reply(self, gmail_followup: dict):
        """run_probe dispatches to gmail_reply probe."""
        from app.followup_probes import run_probe

        with patch("app.followup_probes._build_gmail_service", return_value=None):
            result = run_probe(gmail_followup)
            assert result["struck"] is False

    def test_unknown_kind(self):
        """run_probe returns not-struck for unknown probe kind."""
        from app.followup_probes import run_probe

        followup = {"id": "test", "condition": {"kind": "unknown_probe"}}
        result = run_probe(followup)
        assert result["struck"] is False
        assert "Unknown probe kind" in result["evidence"]

    def test_missing_condition(self):
        """run_probe returns not-struck when condition is missing."""
        from app.followup_probes import run_probe

        followup = {"id": "test"}
        result = run_probe(followup)
        assert result["struck"] is False

    def test_probe_never_throws(self):
        """run_probe never throws an exception."""
        from app.followup_probes import run_probe

        result = run_probe({})
        assert "struck" in result
        assert "evidence" in result
