"""Unit tests for github_tools.create_repo — allowlist guardrail + org/PAT resolution."""

from __future__ import annotations

import httpx
import pytest

from app.tools import github_tools


def test_create_repo_rejects_repo_not_in_allowlist():
    assert "not-allowed-repo" not in github_tools.settings.allowed_repos
    out = github_tools.create_repo(repo="not-allowed-repo")
    assert out["status"] == "error"
    assert "allowed_repos" in out["reason"]


def test_repo_org_defaults_to_truesightdao():
    assert github_tools._repo_org("some_dao_repo") == "TrueSightDAO"


def test_repo_org_honours_override(monkeypatch):
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    assert github_tools._repo_org("getdata-mcp-bridge") == "KrakeIO"


def test_create_repo_rejects_missing_pat(monkeypatch):
    if "getdata-mcp-bridge" not in github_tools.settings.allowed_repos:
        github_tools.settings.allowed_repos.append("getdata-mcp-bridge")
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "")
    try:
        out = github_tools.create_repo(repo="getdata-mcp-bridge")
        assert out["status"] == "error"
        assert "No PAT configured" in out["reason"]
    finally:
        github_tools.settings.allowed_repos.remove("getdata-mcp-bridge")


def test_create_repo_success(monkeypatch):
    if "getdata-mcp-bridge" not in github_tools.settings.allowed_repos:
        github_tools.settings.allowed_repos.append("getdata-mcp-bridge")
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
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
    try:
        out = github_tools.create_repo(repo="getdata-mcp-bridge", private=False, description="test")
        assert out["status"] == "success"
        assert out["org"] == "KrakeIO"
        assert out["url"] == "https://github.com/KrakeIO/getdata-mcp-bridge"
    finally:
        github_tools.settings.allowed_repos.remove("getdata-mcp-bridge")


def test_tool_spec_registered():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("create_repo")
    assert spec is not None
