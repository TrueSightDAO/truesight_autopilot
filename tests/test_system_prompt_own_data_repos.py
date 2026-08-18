"""Unit tests for build_system_prompt()'s "YOUR OWN DATA REPOS" override block."""

from __future__ import annotations

from app import context
from app.config import settings


def test_no_override_block_for_sophias_defaults(monkeypatch):
    monkeypatch.setattr(settings, "transcript_repo", "truesight_autopilot_transcript")
    monkeypatch.setattr(settings, "attachments_repo", "store_interaction_attachments")
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" not in prompt


def test_override_block_appears_when_transcript_repo_differs(monkeypatch):
    monkeypatch.setattr(settings, "transcript_repo", "bionpact_autopilot_transcription")
    monkeypatch.setattr(settings, "attachments_repo", "store_interaction_attachments")
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" in prompt
    assert "bionpact_autopilot_transcription" in prompt


def test_override_block_appears_when_attachments_repo_differs(monkeypatch):
    monkeypatch.setattr(settings, "transcript_repo", "truesight_autopilot_transcript")
    monkeypatch.setattr(settings, "attachments_repo", "bionpact_attachments")
    prompt = context.build_system_prompt()
    assert "YOUR OWN DATA REPOS" in prompt
    assert "bionpact_attachments" in prompt
