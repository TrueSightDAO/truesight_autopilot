"""Unit tests for push_to_personal_repo — registry gating, identity match, vault, guardrails."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from app.tools import personal_git_tools
from app.vault import Vault, reset_vault_for_testing

_REGISTRY_MD = """
# Personal contributor backlogs — per-contributor registry

## Registry

| Contributor | Backlog repo | Format | Vault credential name |
|---|---|---|---|
| Gary Teh | `github.com/garyjob/perch-market-analysis` (private) | `BACKLOG.md` | `PERSONAL_GITHUB_PAT` (in `sophia.truesight.me/vault/`) |
| Ada Lovelace | `github.com/ada/notes` (private) | `BACKLOG.md` | `NOT-A-BACKTICK-NAME` |
"""


@pytest.fixture(autouse=True)
def _fresh_vault():
    with tempfile.TemporaryDirectory(prefix="vault_test_") as tmpdir:
        v = Vault(vault_dir=str(tmpdir))
        v.initialize()
        reset_vault_for_testing(v)
        yield v
        reset_vault_for_testing(None)


@pytest.fixture()
def registry(monkeypatch):
    """Point the registry lookup at the fixture markdown above instead of hitting GitHub."""

    def _fake_read_repo_file(repo, path, ref="main"):
        assert repo == "agentic_ai_context"
        assert path == "PERSONAL_CONTRIBUTOR_BACKLOGS.md"
        return {"type": "file", "content": _REGISTRY_MD}

    monkeypatch.setattr(personal_git_tools, "read_repo_file", _fake_read_repo_file)


@pytest.fixture()
def bare_repo(tmp_path, monkeypatch, registry):
    """A local bare repo seeded with one commit, served over file://, standing in for
    garyjob/perch-market-analysis."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "BACKLOG.md").write_text("# backlog\n\n## Queue\n\n## Log\n")
    env_git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*env_git, "add", "-A"], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        [*env_git, "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=seed, check=True, capture_output=True)

    monkeypatch.setattr(personal_git_tools, "_remote_url", lambda repo: f"file://{bare}")
    yield bare


def _branch_file(bare: Path, branch: str, path: str, tmp_path: Path) -> str:
    check = tmp_path / f"check-{branch.replace('/', '-')}"
    subprocess.run(
        ["git", "clone", "--branch", branch, f"file://{bare}", str(check)],
        check=True,
        capture_output=True,
    )
    return (check / path).read_text()


def test_no_governor_name_is_refused(registry):
    out = personal_git_tools.push_to_personal_repo(
        governor_name="",
        target_repo="garyjob/perch-market-analysis",
        branch="log/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "no calling identity" in out["reason"]


def test_unregistered_contributor_is_refused(registry):
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Someone Unregistered",
        target_repo="whoever/whatever",
        branch="log/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "no registry entry" in out["reason"]


def test_repo_mismatch_is_refused(registry):
    """Gary is registered for perch-market-analysis — must not be able to target another repo,
    including someone ELSE's registered repo."""
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Gary Teh",
        target_repo="ada/notes",
        branch="log/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "does not match the repo registered" in out["reason"]


def test_malformed_registry_row_is_refused(registry):
    """Ada's row has a credential cell with no backtick-quoted name — must refuse, not crash."""
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Ada Lovelace",
        target_repo="ada/notes",
        branch="log/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "no registry entry" in out["reason"]


def test_missing_credential_is_refused(registry):
    """Registry matches, but nothing has been added to the vault yet."""
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Gary Teh",
        target_repo="garyjob/perch-market-analysis",
        branch="log/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "not in the vault yet" in out["reason"]
    assert "PERSONAL_GITHUB_PAT" in out["reason"]


def test_default_branch_push_is_refused(bare_repo, _fresh_vault):
    _fresh_vault.add("PERSONAL_GITHUB_PAT", "fake-token", "test", ["repo"], "Gary Teh")
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Gary Teh",
        target_repo="garyjob/perch-market-analysis",
        branch="main",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "default branch" in out["reason"]


def test_path_traversal_is_rejected(bare_repo, _fresh_vault):
    _fresh_vault.add("PERSONAL_GITHUB_PAT", "fake-token", "test", ["repo"], "Gary Teh")
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Gary Teh",
        target_repo="garyjob/perch-market-analysis",
        branch="log/evil",
        commit_message="m",
        writes=[{"path": "../outside.txt", "content": "nope"}],
        open_pr=False,
    )
    assert out["status"] == "error"
    assert "invalid write path" in out["reason"]


def test_happy_path_writes_and_never_leaks_token(bare_repo, tmp_path, _fresh_vault):
    _fresh_vault.add("PERSONAL_GITHUB_PAT", "sekrit-token-value", "test", ["repo"], "Gary Teh")
    out = personal_git_tools.push_to_personal_repo(
        governor_name="Gary Teh",
        target_repo="garyjob/perch-market-analysis",
        branch="log/2026-07-21",
        commit_message="log entry",
        edits=[
            {"path": "BACKLOG.md", "search": "## Log\n", "replace": "## Log\n\n- test entry\n"}
        ],
        open_pr=False,
    )
    assert out["status"] == "success", out
    assert out["applied"] == ["edit BACKLOG.md"]
    assert "sekrit-token-value" not in str(out)

    content = _branch_file(bare_repo, "log/2026-07-21", "BACKLOG.md", tmp_path)
    assert "- test entry" in content

    # The token must never have been persisted into the clone's own git config either.
    check = tmp_path / "config-check"
    subprocess.run(
        ["git", "clone", "--branch", "log/2026-07-21", f"file://{bare_repo}", str(check)],
        check=True,
        capture_output=True,
    )
    config_text = (check / ".git" / "config").read_text()
    assert "sekrit-token-value" not in config_text


def test_tool_spec_registered():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("push_to_personal_repo")
    assert spec is not None
    assert spec.default_roles == frozenset({"infrastructure"})


def test_policy_classifies_as_write_not_secret():
    """Regression guard: SECRET classification unconditionally denies the tool (see policy.py
    evaluate()) — this tool must stay WRITE (governor-gated) or it would never be callable."""
    from app.policy import ActionClass, classify_action

    assert classify_action("push_to_personal_repo") == ActionClass.WRITE
