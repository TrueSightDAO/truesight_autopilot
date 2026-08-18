"""Unit test for _resolve_followups_md() honoring settings.own_repos["followups"]."""

from __future__ import annotations

from app.config import settings


def test_resolves_own_followups_repo_when_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    monkeypatch.setattr(
        settings,
        "own_repos",
        {**settings.own_repos, "followups": "bionpact_agentic_ai_context"},
    )
    own_repo_dir = tmp_path / "bionpact_agentic_ai_context"
    own_repo_dir.mkdir()
    (own_repo_dir / "OPEN_FOLLOWUPS.md").write_text("# empty queue\n")

    # Also create the public default's dir with a DIFFERENT file present, to
    # prove resolution picks the overridden repo, not the shared default.
    (tmp_path / "agentic_ai_context").mkdir()
    (tmp_path / "agentic_ai_context" / "OPEN_FOLLOWUPS.md").write_text(
        "# should not be picked\n"
    )

    import app.followups as followups_module

    resolved = followups_module._resolve_followups_md()
    assert resolved == own_repo_dir / "OPEN_FOLLOWUPS.md"
    assert resolved.read_text() == "# empty queue\n"
