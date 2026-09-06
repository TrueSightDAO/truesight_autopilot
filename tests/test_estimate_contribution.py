"""Unit tests for scripts/estimate_contribution.py."""

from datetime import datetime

import pytest

from scripts.estimate_contribution import (
    _active_minutes,
    _count_role,
    _governor_name,
    _substantive_governor_msgs,
    DEFAULTS,
)

SAMPLE_HISTORY = [
    {"role": "system", "content": "[ROLE: general]"},
    {
        "role": "user",
        "content": (
            "[GOVERNOR_IDENTITY: You are speaking with Gary Teh. When they say"
            " 'I', 'me', or 'my', they mean Gary Teh.]"
            "[Telegram context: chat_id=-1003919341801, thread_id=22433]"
            "Please estimate time for this session."
        ),
    },
    {"role": "assistant", "content": "Let me look.", "tool_calls": [{}]},
    {"role": "tool", "content": "result"},
    {"role": "assistant", "content": "Done."},
    {"role": "user", "content": "[emoji-go: ok]"},  # wrapper-only: not substantive
]


def test_count_role():
    assert _count_role(SAMPLE_HISTORY, "tool") == 1
    assert _count_role(SAMPLE_HISTORY, "assistant") == 2


def test_substantive_governor_msgs_excludes_wrappers():
    msgs = _substantive_governor_msgs(SAMPLE_HISTORY)
    assert len(msgs) == 1
    assert "Please estimate time" in msgs[0]


def test_governor_name():
    assert _governor_name(SAMPLE_HISTORY) == "Gary Teh"


def test_active_minutes_trims_idle_gaps():
    ts = [
        datetime.fromisoformat("2026-09-05T10:00:00"),
        datetime.fromisoformat("2026-09-05T10:03:00"),  # 3 min gap
        datetime.fromisoformat("2026-09-05T12:00:00"),  # 117 min idle
    ]
    # 3 min kept, 117 -> capped at gap_trim_min (5)
    assert _active_minutes(ts, DEFAULTS["gap_trim_min"]) == pytest.approx(8.0)
