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


# ── get_escalate_after_days tests ───────────────────────────────────────


class TestGetEscalateAfterDays:
    def test_reads_from_schedule_canonical_location(self):
        """schedule.escalate_after_days is the schema's documented location
        (see followups.py module docstring) and takes priority."""
        from app.followup_probes import get_escalate_after_days

        followup = {"schedule": {"escalate_after_days": 30}, "condition": {}}
        assert get_escalate_after_days(followup) == 30

    def test_falls_back_to_condition(self):
        """Root cause of the 2026-07-24 immediate-strike bug: both real
        OPEN_FOLLOWUPS.md entries at the time (chocolate-subscription-phase2,
        warmup-conversion-30day-readout) had escalate_after_days nested under
        `condition`, not `schedule` -- the schema's canonical location. Every
        reader silently fell back to the hardcoded default of 1 day instead,
        so both follow-ups struck almost immediately rather than after their
        intended 60/30-day window. Must be read correctly from either spot."""
        from app.followup_probes import get_escalate_after_days

        followup = {"schedule": {"check": "weekly"}, "condition": {"escalate_after_days": 60}}
        assert get_escalate_after_days(followup) == 60

    def test_schedule_takes_priority_over_condition(self):
        """If (mistakenly) present in both, schedule wins -- it's canonical."""
        from app.followup_probes import get_escalate_after_days

        followup = {
            "schedule": {"escalate_after_days": 30},
            "condition": {"escalate_after_days": 999},
        }
        assert get_escalate_after_days(followup) == 30

    def test_defaults_to_one_when_absent_from_both(self):
        from app.followup_probes import get_escalate_after_days

        followup = {"schedule": {}, "condition": {}}
        assert get_escalate_after_days(followup) == 1

    def test_missing_schedule_and_condition_keys_entirely(self):
        from app.followup_probes import get_escalate_after_days

        assert get_escalate_after_days({}) == 1


# ── elapsed_days tests ───────────────────────────────────────────────────


class TestElapsedDays:
    def test_not_yet_elapsed(self, elapsed_followup: dict):
        """Returns struck=False when within the threshold."""
        from app.followup_probes import elapsed_days

        now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(elapsed_followup, now)
        assert result["struck"] is False

    def test_escalate_after_days_nested_under_condition_still_respected(self):
        """Integration guard for the exact real-world shape that misfired
        2026-07-24: escalate_after_days nested under `condition` (not
        `schedule`) must still gate the threshold correctly, not silently
        fall back to 1 day and strike immediately."""
        from app.followup_probes import elapsed_days

        followup = {
            "id": "test-condition-nested",
            "created_at": "2026-06-11",
            "condition": {"kind": "elapsed_days", "escalate_after_days": 60},
            "schedule": {"check": "weekly", "on_escalate": "ping_thread"},
        }
        # 43 days elapsed — past the old buggy default (1) but well short of
        # the actually-configured 60-day threshold.
        now = datetime(2026, 7, 24, 0, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(followup, now)
        assert result["struck"] is False, result

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

    def test_end_to_end_through_real_yaml_parse(self):
        """Regression guard for the 2026-07-24 silent-follow-up bug: every
        other test in this file hand-builds the followup dict with
        created_at as a Python str literal, which never exercises the
        yaml.safe_load boundary where the actual bug lived (an unquoted
        `created_at: 2026-06-11` parses to a datetime.date, not a str).
        This goes through the real parser — app.followups._parse_block —
        exactly as OPEN_FOLLOWUPS.md is authored, then feeds that parsed
        dict into the real probe."""
        from app.followup_probes import elapsed_days
        from app.followups import _parse_block

        body = (
            "id: test-real-parse\n"
            "chat_id: -1003919341801\n"
            "thread_id: 42\n"
            "title: End to end parse test\n"
            "created_at: 2026-06-10\n"
            "condition:\n"
            "  kind: elapsed_days\n"
            "schedule:\n"
            "  check: daily\n"
            "  escalate_after_days: 2\n"
            "  on_escalate: ping_thread\n"
            "status: open\n"
        )
        parsed = _parse_block(body, 1)
        assert isinstance(parsed, dict), parsed  # not an error string

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
        result = elapsed_days(parsed, now)
        assert result["struck"] is True, result

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
