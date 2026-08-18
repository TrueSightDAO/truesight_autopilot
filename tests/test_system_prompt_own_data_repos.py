"""Unit tests for build_system_prompt()'s "YOUR OWN DATA REPOS" override block."""

from __future__ import annotations

from app import context
from app.config import settings

_SOPHIA_DEFAULTS = {
    "context": "agentic_ai_context",
    "transcript": "truesight_autopilot_transcript",
    "attachments": "store_interaction_attachments",
    "followups": "agentic_ai_context",
}


def test_no_override_block_for_sophias_defaults(monkeypatch):
    monkeypatch.setattr(settings, "own_repos", dict(_SOPHIA_DEFAULTS))
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" not in prompt


def test_override_block_appears_when_transcript_repo_differs(monkeypatch):
    monkeypatch.setattr(
        settings,
        "own_repos",
        {**_SOPHIA_DEFAULTS, "transcript": "bionpact_autopilot_transcription"},
    )
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" in prompt
    assert "bionpact_autopilot_transcription" in prompt


def test_override_block_appears_when_attachments_repo_differs(monkeypatch):
    monkeypatch.setattr(
        settings,
        "own_repos",
        {**_SOPHIA_DEFAULTS, "attachments": "bionpact_attachments"},
    )
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" in prompt
    assert "bionpact_attachments" in prompt


def test_override_block_appears_when_followups_repo_differs(monkeypatch):
    monkeypatch.setattr(
        settings,
        "own_repos",
        {**_SOPHIA_DEFAULTS, "followups": "bionpact_agentic_ai_context"},
    )
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" in prompt
    assert "bionpact_agentic_ai_context" in prompt
