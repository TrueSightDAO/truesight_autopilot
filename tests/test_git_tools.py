"""Unit tests for git_push_changes — guardrails + a real file:// round-trip."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.tools import git_tools


@pytest.fixture()
def bare_repo(tmp_path, monkeypatch):
    """A local bare repo seeded with one commit, served over file://."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(bare), str(seed)], check=True, capture_output=True
    )
    (seed / "README.md").write_text("# fixture\n\nhello world\n")
    big = "x" * 40_000  # >15KB — the size class the Contents-API tools choked on
    (seed / "big.html").write_text(f"<html>{big}<!-- MARKER --></html>\n")
    env_git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*env_git, "add", "-A"], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        [*env_git, "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:main"],
        cwd=seed,
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr(git_tools, "_remote_url", lambda repo: f"file://{bare}")
    monkeypatch.setattr(git_tools.settings, "github_pat", "test-pat")
    if "fixture-repo" not in git_tools.settings.allowed_repos:
        git_tools.settings.allowed_repos.append("fixture-repo")
    yield bare
    if "fixture-repo" in git_tools.settings.allowed_repos:
        git_tools.settings.allowed_repos.remove("fixture-repo")


def _branch_file(bare: Path, branch: str, path: str, tmp_path: Path) -> str:
    check = tmp_path / f"check-{branch.replace('/', '-')}-{path.replace('/', '-')}"
    subprocess.run(
        ["git", "clone", "--branch", branch, f"file://{bare}", str(check)],
        check=True,
        capture_output=True,
    )
    return (check / path).read_text()


def test_repo_not_in_allowlist_is_rejected():
    out = git_tools.git_push_changes(
        repo="some-rando-repo",
        branch="f/x",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
    )
    assert out["status"] == "error"
    assert "allowed" in out["reason"]


def test_default_branch_push_is_refused(bare_repo):
    out = git_tools.git_push_changes(
        repo="fixture-repo",
        branch="main",
        commit_message="m",
        writes=[{"path": "a.txt", "content": "a"}],
        open_pr=False,
    )
    assert out["status"] == "error"
    assert "default branch" in out["reason"]


def test_path_traversal_is_rejected(bare_repo):
    out = git_tools.git_push_changes(
        repo="fixture-repo",
        branch="f/evil",
        commit_message="m",
        writes=[{"path": "../outside.txt", "content": "nope"}],
        open_pr=False,
    )
    assert out["status"] == "error"
    assert "invalid write path" in out["reason"]


def test_write_edit_delete_round_trip(bare_repo, tmp_path):
    out = git_tools.git_push_changes(
        repo="fixture-repo",
        branch="feature/round-trip",
        commit_message="round trip",
        writes=[{"path": "docs/new.md", "content": "fresh\n"}],
        edits=[
            {
                "path": "big.html",
                "search": "<!-- MARKER -->",
                "replace": "<!-- EDITED -->",
            }
        ],
        deletes=["README.md"],
        open_pr=False,
    )
    assert out["status"] == "success", out
    assert sorted(out["applied"]) == [
        "delete README.md",
        "edit big.html",
        "write docs/new.md",
    ]

    assert (
        _branch_file(bare_repo, "feature/round-trip", "docs/new.md", tmp_path)
        == "fresh\n"
    )
    assert "<!-- EDITED -->" in _branch_file(
        bare_repo, "feature/round-trip", "big.html", tmp_path
    )


def test_ambiguous_edit_requires_replace_all(bare_repo):
    out = git_tools.git_push_changes(
        repo="fixture-repo",
        branch="f/ambiguous",
        commit_message="m",
        edits=[
            {"path": "big.html", "search": "x", "replace": "y"}
        ],  # thousands of hits
        open_pr=False,
    )
    assert out["status"] == "error"
    assert "replace_all" in out["reason"]


def test_edit_search_not_found(bare_repo):
    out = git_tools.git_push_changes(
        repo="fixture-repo",
        branch="f/missing",
        commit_message="m",
        edits=[{"path": "big.html", "search": "NOT-THERE-AT-ALL", "replace": "y"}],
        open_pr=False,
    )
    assert out["status"] == "error"
    assert "not found" in out["reason"]


def test_tool_spec_registered():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("git_push_changes")
    assert spec is not None
    assert spec.default_roles == frozenset({"infrastructure"})


def test_repo_org_defaults_to_truesightdao():
    assert git_tools._repo_org("some_dao_repo") == "TrueSightDAO"


def test_repo_org_honours_override(monkeypatch):
    monkeypatch.setitem(git_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    assert git_tools._repo_org("getdata-mcp-bridge") == "KrakeIO"


def test_repo_pat_picks_matching_org(monkeypatch):
    monkeypatch.setitem(git_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    monkeypatch.setattr(git_tools.settings, "krake_io_pat", "krake-test-pat")
    monkeypatch.setattr(git_tools.settings, "github_pat", "dao-test-pat")
    assert git_tools._repo_pat("getdata-mcp-bridge") == "krake-test-pat"
    assert git_tools._repo_pat("some_dao_repo") == "dao-test-pat"


def test_remote_url_uses_resolved_org(monkeypatch):
    monkeypatch.setitem(git_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    assert git_tools._remote_url("getdata-mcp-bridge") == "git@github.com:KrakeIO/getdata-mcp-bridge.git"
    assert git_tools._remote_url("some_dao_repo") == "git@github.com:TrueSightDAO/some_dao_repo.git"
