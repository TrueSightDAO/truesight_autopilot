"""Unit tests for github_tools.create_repo — pattern guardrail + org/PAT resolution."""

from __future__ import annotations

import httpx

from app.tools import github_tools


def test_create_repo_rejects_unblessed_name():
    """A name matching no create_repo pattern is refused (hallucinated repo)."""
    out = github_tools.create_repo(repo="hallucinated-xyz")
    assert out["status"] == "error"
    assert "pattern" in out["reason"]


def test_create_repo_accepts_blessed_pattern_name(monkeypatch):
    """A name matching a blessed glob passes the gate (PAT checked next)."""
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "")
    out = github_tools.create_repo(repo="butterfly-effect-club-program")
    # Pattern gate passed -> it proceeded to the PAT check, not the guard.
    assert out["status"] == "error"
    assert "pattern" not in out["reason"]


def test_repo_org_defaults_to_truesightdao():
    assert github_tools._repo_org("some_dao_repo") == "TrueSightDAO"


def test_repo_org_honours_override(monkeypatch):
    monkeypatch.setitem(
        github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO"
    )
    assert github_tools._repo_org("getdata-mcp-bridge") == "KrakeIO"


def test_create_repo_rejects_missing_pat(monkeypatch):
    monkeypatch.setattr(github_tools.settings, "create_repo_patterns", ["getdata-*"])
    monkeypatch.setitem(
        github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO"
    )
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "")
    out = github_tools.create_repo(repo="getdata-mcp-bridge")
    assert out["status"] == "error"
    assert "No PAT configured" in out["reason"]


def test_create_repo_success(monkeypatch):
    monkeypatch.setattr(github_tools.settings, "create_repo_patterns", ["getdata-*"])
    monkeypatch.setitem(
        github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO"
    )
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "test-krake-pat")

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            pass

        def json(self):
            return {"html_url": "https://github.com/KrakeIO/getdata-mcp-bridge"}

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url == "https://api.github.com/orgs/KrakeIO/repos"
        assert headers["Authorization"] == "Bearer test-krake-pat"
        assert json["name"] == "getdata-mcp-bridge"
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = github_tools.create_repo(
        repo="getdata-mcp-bridge", private=False, description="test"
    )
    assert out["status"] == "success"
    assert out["org"] == "KrakeIO"
    assert out["url"] == "https://github.com/KrakeIO/getdata-mcp-bridge"
