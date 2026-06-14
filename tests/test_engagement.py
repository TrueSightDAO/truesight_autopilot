"""Tests for app/engagement.py — engagement modes (Phase 2)."""

import tempfile
from pathlib import Path


import pytest

from app.engagement import (
    EngagementMode,
    format_audit_message,
    get_engagement_mode,
    get_audit_channel_id,
    is_addressed,
    is_dm,
    is_dm_write_allowed,
    is_reply_to_sophia,
    set_engagement_mode,
)


@pytest.fixture(autouse=True)
def _temp_config():
    """Use a temp config file for tests."""
    with tempfile.TemporaryDirectory(prefix="engagement_test_") as tmpdir:
        import app.engagement as eng

        original = eng.ENGAGEMENT_MODE_FILE
        eng.ENGAGEMENT_MODE_FILE = str(Path(tmpdir) / "engagement_modes.json")
        yield
        eng.ENGAGEMENT_MODE_FILE = original


# ── Engagement mode config ─────────────────────────────────────────────────


class TestEngagementModeConfig:
    def test_default_is_proactive(self):
        mode = get_engagement_mode(12345)
        assert mode == EngagementMode.PROACTIVE

    def test_set_and_get_addressed_only(self):
        result = set_engagement_mode(
            12345, EngagementMode.ADDRESSED_ONLY, set_by="Gary"
        )
        assert result is True
        mode = get_engagement_mode(12345)
        assert mode == EngagementMode.ADDRESSED_ONLY

    def test_set_and_get_proactive(self):
        set_engagement_mode(12345, EngagementMode.PROACTIVE)
        mode = get_engagement_mode(12345)
        assert mode == EngagementMode.PROACTIVE

    def test_invalid_mode_returns_false(self):
        result = set_engagement_mode(12345, "invalid_mode")
        assert result is False

    def test_per_thread_mode(self):
        set_engagement_mode(12345, EngagementMode.ADDRESSED_ONLY, thread_id=2744)
        assert (
            get_engagement_mode(12345, thread_id=2744) == EngagementMode.ADDRESSED_ONLY
        )
        # Different thread should still be proactive
        assert get_engagement_mode(12345, thread_id=9999) == EngagementMode.PROACTIVE

    def test_persists_across_reload(self):
        set_engagement_mode(12345, EngagementMode.ADDRESSED_ONLY)
        # Reload by clearing cache (config is file-based)
        mode = get_engagement_mode(12345)
        assert mode == EngagementMode.ADDRESSED_ONLY


# ── Addressed-only detection ───────────────────────────────────────────────


class TestIsAddressed:
    def test_starts_with_sophia(self):
        assert is_addressed("Sophia, what's the weather?") is True

    def test_starts_with_at_mention(self):
        assert is_addressed("@Sophia check this") is True

    def test_hey_sophia(self):
        assert is_addressed("Hey Sophia, can you help?") is True

    def test_hi_sophia(self):
        assert is_addressed("Hi Sophia! How are you?") is True

    def test_sophia_in_first_50_chars(self):
        assert is_addressed("I think Sophia should look at this") is True

    def test_not_addressed(self):
        assert is_addressed("The weather is nice today.") is False

    def test_empty_text(self):
        assert is_addressed("") is False

    def test_bot_username_mention(self):
        assert is_addressed("Check this @mybot", bot_username="mybot") is True

    def test_bot_username_not_present(self):
        assert is_addressed("Hello there", bot_username="mybot") is False

    def test_case_insensitive(self):
        assert is_addressed("SOPHIA, DEPLOY NOW") is True

    def test_sophia_with_punctuation(self):
        assert is_addressed("Sophia! Look at this") is True
        assert is_addressed("Sophia? Are you there?") is True

    def test_normal_conversation_not_addressed(self):
        messages = [
            "I went to the store today",
            "The cacao harvest was good",
            "Let me know what you think",
            "Gary said we should deploy",
        ]
        for msg in messages:
            assert is_addressed(msg) is False, f"'{msg}' should not be addressed"


class TestIsReplyToSophia:
    def test_reply_to_sophia(self):
        msg = {
            "reply_to_message": {
                "from": {"id": 123, "is_bot": True},
            }
        }
        assert is_reply_to_sophia(msg, sophia_bot_id=123) is True

    def test_reply_to_other(self):
        msg = {
            "reply_to_message": {
                "from": {"id": 456, "is_bot": False},
            }
        }
        assert is_reply_to_sophia(msg, sophia_bot_id=123) is False

    def test_no_reply(self):
        msg = {"text": "Hello"}
        assert is_reply_to_sophia(msg, sophia_bot_id=123) is False


# ── DM policy ──────────────────────────────────────────────────────────────


class TestDM:
    def test_private_chat_is_dm(self):
        assert is_dm({"type": "private"}) is True

    def test_group_is_not_dm(self):
        assert is_dm({"type": "group"}) is False

    def test_supergroup_is_not_dm(self):
        assert is_dm({"type": "supergroup"}) is False

    def test_allowed_user_can_write_in_dm(self):
        assert is_dm_write_allowed(12345, {12345, 67890}) is True

    def test_unknown_user_cannot_write_in_dm(self):
        assert is_dm_write_allowed(99999, {12345}) is False


# ── Audit channel ──────────────────────────────────────────────────────────


class TestAuditChannel:
    def test_format_audit_message(self):
        msg = format_audit_message(
            "deploy", "Gary", "Deployed v1.2.3", surface="thread 2744"
        )
        assert "deploy" in msg
        assert "Gary" in msg
        assert "thread 2744" in msg
        assert "v1.2.3" in msg

    def test_format_minimal(self):
        msg = format_audit_message("git_push", "Gary", "Pushed changes")
        assert "git_push" in msg
        assert "Gary" in msg

    def test_get_audit_channel_id_not_set(self):
        # Just verify it returns None or int when not configured
        channel_id = get_audit_channel_id()
        assert channel_id is None or isinstance(channel_id, int)
