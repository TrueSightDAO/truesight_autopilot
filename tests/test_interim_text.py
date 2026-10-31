"""Guard (2026-10-31): interim progress placeholders must never be resume-awaiting.

The per-turn ``Thinking...`` placeholder was posted through ``send_message``,
which flagged it resume-awaiting. On any turn longer than
``DEFAULT_AWAITING_FRESH_S`` (3600s) that placeholder aged from a *fresh*
prompt into ``awaiting_stale``, so the loop nagged the governor about a
decision that did not exist.
"""

from __future__ import annotations

import pytest

from app.interim_text import is_interim_text
from app.loop_status import parse_awaiting

INTERIM = [
    "\U0001f504 Thinking\u2026",
    "\U0001f504 Thinking\u2026 (round 5)",
    "\U0001f4c4 Processing attachment\u2026",
]

NOT_INTERIM = [
    # The first draft matched "<emoji> (Thinking|Running|...)" and therefore
    # also swallowed these. Suppressing the mark on a real reply silently
    # removes the governor's reaction-to-resume affordance.
    "\U0001f504 Running the deploy now, will report",
    "\U0001f504 Thinking about it -- I'll ship both units",
    "\u2699\ufe0f ssh run ...",
    "Done \u2014 report attached",
    "\u2705 Merged PR #621",
    "Yes, go ahead",
    "",
]


@pytest.mark.parametrize("text", INTERIM)
def test_exact_placeholders_are_interim(text: str) -> None:
    assert is_interim_text(text) is True


@pytest.mark.parametrize("text", NOT_INTERIM)
def test_real_messages_are_not_interim(text: str) -> None:
    assert is_interim_text(text) is False


def test_parse_awaiting_ignores_legacy_placeholder_entries() -> None:
    """Already-persisted placeholder rows must not nag before their TTL lapses."""
    raw = {
        "placeholder": {
            "channel_id": "999",
            "text": "\U0001f504 Thinking\u2026",
            "ts": 1000,
        },
        "question": {"channel_id": "999", "text": "Which repo?", "ts": 900},
        "placeholder_only": {
            "channel_id": "888",
            "text": "\U0001f504 Thinking\u2026",
            "ts": 1000,
        },
    }
    out = parse_awaiting(raw, now=2000)
    assert out == {"999": 900.0}


def test_send_message_marks_real_reply_but_not_placeholder(monkeypatch) -> None:
    """Auto-detect: a real reply still gets the reaction go-signal."""
    from app import discord_adapter

    marked: list[str] = []
    monkeypatch.setattr(discord_adapter.settings, "discord_dry_run", False)
    monkeypatch.setattr(
        discord_adapter,
        "_api",
        lambda *a, **k: {"id": "msg-1"},
    )
    monkeypatch.setattr(
        discord_adapter.discord_resume_registry,
        "mark_resume_awaiting",
        lambda mid, cid, text: marked.append(str(mid)),
    )

    discord_adapter.send_message("123", "\U0001f504 Thinking\u2026")
    assert marked == []

    discord_adapter.send_message("123", "Here is the report you asked for")
    assert marked == ["msg-1"]

    marked.clear()
    discord_adapter.send_message("123", "\U0001f504 Thinking\u2026", mark_resume=True)
    assert marked == ["msg-1"]
