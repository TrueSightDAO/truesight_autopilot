"""Unit tests for Settings.transcript_repo / attachments_repo / github_read_pat.

Confirms the defaults preserve Sophia's existing hardcoded behavior (§5d —
zero behavior change), and that overriding them (as a sibling locked-down
instance would via its own .env) folds cleanly into api_only_repos.
"""

from __future__ import annotations

from app.config import Settings


def test_defaults_match_sophias_existing_hardcoded_repos():
    s = Settings()
    assert s.transcript_repo == "truesight_autopilot_transcript"
    assert s.attachments_repo == "store_interaction_attachments"
    assert s.github_read_pat == ""


def test_defaults_are_noop_on_api_only_repos():
    """Both defaults are already literal entries in api_only_repos — the
    model_validator must not introduce duplicates for Sophia."""
    s = Settings()
    assert s.api_only_repos.count("truesight_autopilot_transcript") == 1
    assert s.api_only_repos.count("store_interaction_attachments") == 1


def test_overriding_both_repos_folds_into_api_only_repos():
    s = Settings(
        TRANSCRIPT_REPO="bionpact_autopilot_transcription",
        ATTACHMENTS_REPO="bionpact_attachments",
    )
    assert s.transcript_repo == "bionpact_autopilot_transcription"
    assert s.attachments_repo == "bionpact_attachments"
    assert "bionpact_autopilot_transcription" in s.api_only_repos
    assert "bionpact_attachments" in s.api_only_repos
    # the validator only appends — Sophia's literal defaults stay in the
    # shared reference list too (harmless; they're just taxonomy entries).
    assert "truesight_autopilot_transcript" in s.api_only_repos
    assert "store_interaction_attachments" in s.api_only_repos
