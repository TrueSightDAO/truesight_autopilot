"""
Tests for app/followup_loop.py — follow-up comb loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_followup() -> dict:
    """A basic follow-up for loop testing."""
    return {
        "id": "test-followup",
        "chat_id": "-1003919341801",
        "thread_id": 2622,
        "title": "Test follow-up",
        "created_at": "2026-06-10",
        "condition": {"kind": "elapsed_days"},
        "schedule": {
            "check": "daily",
            "escalate_after_days": 2,
            "on_escalate": "ping_thread",
        },
        "status": "open",
    }


# ── helper tests ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_compute_next_check_daily(self):
        """_compute_next_check returns ~1 hour from now for daily."""
        from app.followup_loop import _compute_next_check

        followup = {"schedule": {"check": "daily"}}
        result = _compute_next_check(followup)
        # Should be an ISO datetime string
        assert "T" in result
        assert result.endswith("+00:00") or "+" in result

    def test_compute_next_check_weekly(self):
        """_compute_next_check returns ~7 days from now for weekly."""
        from app.followup_loop import _compute_next_check

        followup = {"schedule": {"check": "weekly"}}
        result = _compute_next_check(followup)
        assert "T" in result

    def test_get_thread_id_int(self):
        """_get_thread_id handles int thread_id."""
        from app.followup_loop import _get_thread_id

        assert _get_thread_id({"thread_id": 2622}) == "2622"

    def test_get_thread_id_str(self):
        """_get_thread_id handles str thread_id."""
        from app.followup_loop import _get_thread_id

        assert _get_thread_id({"thread_id": "2622"}) == "2622"

    def test_get_thread_id_none(self):
        """_get_thread_id returns None for missing thread_id."""
        from app.followup_loop import _get_thread_id

        assert _get_thread_id({}) is None

    def test_build_strike_message(self, sample_followup: dict):
        """_build_strike_message includes title and evidence."""
        from app.followup_loop import _build_strike_message

        probe_result = {"struck": True, "evidence": "Found matching email"}
        message = _build_strike_message(sample_followup, probe_result)
        assert "Test follow-up" in message
        assert "Found matching email" in message
        assert "🔔" in message

    def test_build_escalation_message(self, sample_followup: dict):
        """_build_escalation_message includes title and created_at."""
        from app.followup_loop import _build_escalation_message

        message = _build_escalation_message(sample_followup)
        assert "Test follow-up" in message
        assert "2026-06-10" in message
        assert "⏰" in message


# ── _process_one tests ───────────────────────────────────────────────────


class TestProcessOne:
    @pytest.mark.asyncio
    async def test_strike_resolves_followup(self, sample_followup: dict):
        """When probe strikes, follow-up is resolved."""
        from app.followup_loop import _process_one

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch("app.followup_loop.get_state", return_value={"attempts": 0}),
            patch(
                "app.followup_loop.run_probe",
                return_value={"struck": True, "evidence": "Struck!"},
            ),
            patch("app.followup_loop.set_status") as mock_set_status,
            patch("app.followup_loop.upsert_state"),
            patch("app.followup_loop._post_to_thread", new_callable=AsyncMock),
            patch("app.followup_loop._spin_sophia_turn", new_callable=AsyncMock),
        ):
            await _process_one(sample_followup, now)
            mock_set_status.assert_called_once_with("test-followup", "resolved")

    @pytest.mark.asyncio
    async def test_escalation_pings_thread(self, sample_followup: dict):
        """When escalation day reached with no strike, thread is pinged once."""
        from app.followup_loop import _process_one

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch(
                "app.followup_loop.get_state",
                return_value={"attempts": 0, "last_pinged": None},
            ),
            patch(
                "app.followup_loop.run_probe",
                return_value={"struck": False, "evidence": "No reply"},
            ),
            patch(
                "app.followup_loop._post_to_thread", new_callable=AsyncMock
            ) as mock_post,
            patch("app.followup_loop.upsert_state"),
        ):
            await _process_one(sample_followup, now)
            # Should have posted to thread
            assert mock_post.call_count >= 1

    @pytest.mark.asyncio
    async def test_escalation_does_not_reping(self, sample_followup: dict):
        """When already pinged, thread is NOT pinged again."""
        from app.followup_loop import _process_one

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch(
                "app.followup_loop.get_state",
                return_value={
                    "attempts": 0,
                    "last_pinged": "2026-06-12T12:00:00+00:00",
                },
            ),
            patch(
                "app.followup_loop.run_probe",
                return_value={"struck": False, "evidence": "No reply"},
            ),
            patch(
                "app.followup_loop._post_to_thread", new_callable=AsyncMock
            ) as mock_post,
            patch("app.followup_loop.upsert_state"),
        ):
            await _process_one(sample_followup, now)
            # Should NOT have posted (already pinged)
            assert mock_post.call_count == 0

    @pytest.mark.asyncio
    async def test_no_strike_no_escalation(self, sample_followup: dict):
        """When no strike and not yet escalation day, nothing happens."""
        from app.followup_loop import _process_one

        # created_at is 2026-06-10, escalate_after_days=2
        # At 2026-06-11, only 1 day has passed — no escalation
        now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch("app.followup_loop.get_state", return_value={"attempts": 0}),
            patch(
                "app.followup_loop.run_probe",
                return_value={"struck": False, "evidence": "Not yet"},
            ),
            patch(
                "app.followup_loop._post_to_thread", new_callable=AsyncMock
            ) as mock_post,
            patch("app.followup_loop.set_status") as mock_set_status,
            patch("app.followup_loop.upsert_state"),
        ):
            await _process_one(sample_followup, now)
            mock_set_status.assert_not_called()
            assert mock_post.call_count == 0

    @pytest.mark.asyncio
    async def test_updates_state(self, sample_followup: dict):
        """State is updated after processing."""
        from app.followup_loop import _process_one

        now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch("app.followup_loop.get_state", return_value={"attempts": 0}),
            patch(
                "app.followup_loop.run_probe",
                return_value={"struck": False, "evidence": "Not yet"},
            ),
            patch("app.followup_loop._post_to_thread", new_callable=AsyncMock),
            patch("app.followup_loop.upsert_state") as mock_upsert,
        ):
            await _process_one(sample_followup, now)
            mock_upsert.assert_called_once()
            args, kwargs = mock_upsert.call_args
            assert args[0] == "test-followup"
            assert kwargs.get("attempts") == 1


# ── _tick tests ──────────────────────────────────────────────────────────


class TestTick:
    @pytest.mark.asyncio
    async def test_no_due_followups(self):
        """Tick does nothing when no follow-ups are due."""
        from app.followup_loop import _tick

        with patch("app.followup_loop.next_due", return_value=[]):
            result = await _tick()
            assert result is None

    @pytest.mark.asyncio
    async def test_processes_due_followups(self, sample_followup: dict):
        """Tick processes due follow-ups."""
        from app.followup_loop import _tick

        with (
            patch("app.followup_loop.next_due", return_value=[sample_followup]),
            patch(
                "app.followup_loop._process_one", new_callable=AsyncMock
            ) as mock_process,
        ):
            await _tick()
            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_survives_exception(self, sample_followup: dict):
        """Tick survives a probe exception gracefully."""
        from app.followup_loop import _tick

        with (
            patch("app.followup_loop.next_due", return_value=[sample_followup]),
            patch("app.followup_loop._process_one", side_effect=Exception("Boom")),
            patch("app.followup_loop.upsert_state"),
        ):
            # Should not raise
            await _tick()


# ── followup_loop tests ──────────────────────────────────────────────────


class TestFollowupLoop:
    @pytest.mark.asyncio
    async def test_loop_runs_tick(self):
        """followup_loop runs _tick and then sleeps."""
        from app.followup_loop import followup_loop

        with (
            patch("app.followup_loop._tick", new_callable=AsyncMock) as mock_tick,
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            # Run one iteration then stop
            mock_sleep.side_effect = KeyboardInterrupt

            with pytest.raises(KeyboardInterrupt):
                await followup_loop(interval_seconds=1)

            mock_tick.assert_called_once()
            mock_sleep.assert_called_once_with(1)
