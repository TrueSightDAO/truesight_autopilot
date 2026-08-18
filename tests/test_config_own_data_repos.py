"""Unit tests for Settings.own_repos / github_read_pat.

Confirms the defaults preserve Sophia's existing hardcoded behavior (§5d —
zero behavior change), that a partial OWN_REPOS override merges onto the
defaults instead of replacing them, and that transcript/attachments fold
cleanly into api_only_repos.
"""

from __future__ import annotations

from app.config import Settings


def test_defaults_match_sophias_existing_hardcoded_repos():
    s = Settings()
    assert s.own_repos == {
        "context": "agentic_ai_context",
        "transcript": "truesight_autopilot_transcript",
        "attachments": "store_interaction_attachments",
        "followups": "agentic_ai_context",
    }
    assert s.github_read_pat == ""


def test_defaults_are_noop_on_api_only_repos():
    """Both defaults are already literal entries in api_only_repos — the
    model_validator must not introduce duplicates for Sophia."""
    s = Settings()
    assert s.api_only_repos.count("truesight_autopilot_transcript") == 1
    assert s.api_only_repos.count("store_interaction_attachments") == 1


def test_partial_override_merges_onto_defaults():
    """A sibling instance overriding only transcript/attachments/followups
    keeps "context" at the shared default — the documented .env.example
    contract (only override what you're changing)."""
    s = Settings(
        OWN_REPOS='{"transcript":"bionpact_autopilot_transcription",'
        '"attachments":"bionpact_attachments",'
        '"followups":"bionpact_agentic_ai_context"}'
    )
    assert s.own_repos["context"] == "agentic_ai_context"
    assert s.own_repos["transcript"] == "bionpact_autopilot_transcription"
    assert s.own_repos["attachments"] == "bionpact_attachments"
    assert s.own_repos["followups"] == "bionpact_agentic_ai_context"


def test_overriding_transcript_and_attachments_folds_into_api_only_repos():
    s = Settings(
        OWN_REPOS='{"transcript":"bionpact_autopilot_transcription",'
        '"attachments":"bionpact_attachments"}'
    )
    assert "bionpact_autopilot_transcription" in s.api_only_repos
    assert "bionpact_attachments" in s.api_only_repos
    # the validator only appends — Sophia's literal defaults stay in the
    # shared reference list too (harmless; they're just taxonomy entries).
    assert "truesight_autopilot_transcript" in s.api_only_repos
    assert "store_interaction_attachments" in s.api_only_repos
