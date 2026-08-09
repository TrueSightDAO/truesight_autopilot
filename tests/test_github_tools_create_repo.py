"""Unit tests for github_tools.create_repo / enable_github_pages —
confirm_write guardrail + org/PAT resolution.
"""

from __future__ import annotations

import httpx
import pytest

from app.tools import github_tools


def test_create_repo_rejects_without_confirm_write():
    out = github_tools.create_repo(repo="some-new-repo")
    assert out["status"] == "error"
    assert "confirm_write" in out["reason"]


def test_repo_org_defaults_to_truesightdao():
    assert github_tools._repo_org("some_dao_repo") == "TrueSightDAO"


def test_repo_org_honours_override(monkeypatch):
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    assert github_tools._repo_org("getdata-mcp-bridge") == "KrakeIO"


def test_create_repo_rejects_missing_pat(monkeypatch):
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "")
    out = github_tools.create_repo(repo="getdata-mcp-bridge", confirm_write=True)
    assert out["status"] == "error"
    assert "No PAT configured" in out["reason"]


def test_create_repo_success(monkeypatch):
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
    out = github_tools.create_repo(
        repo="getdata-mcp-bridge", private=False, description="test", confirm_write=True
    )
    assert out["status"] == "success"
    assert out["org"] == "KrakeIO"
    assert out["url"] == "https://github.com/KrakeIO/getdata-mcp-bridge"


def test_tool_spec_registered():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("create_repo")
    assert spec is not None


def test_enable_github_pages_rejects_without_confirm_write():
    out = github_tools.enable_github_pages(repo="some-repo")
    assert out["status"] == "error"
    assert "confirm_write" in out["reason"]


def test_enable_github_pages_rejects_missing_pat(monkeypatch):
    monkeypatch.setitem(github_tools.settings.repo_org_overrides, "getdata-mcp-bridge", "KrakeIO")
    monkeypatch.setattr(github_tools.settings, "krake_io_pat", "")
    out = github_tools.enable_github_pages(repo="getdata-mcp-bridge", confirm_write=True)
    assert out["status"] == "error"
    assert "No PAT configured" in out["reason"]


def test_enable_github_pages_success(monkeypatch):
    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "html_url": "https://truesightdao.github.io/some-repo/",
                "status": "building",
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url == "https://api.github.com/repos/TrueSightDAO/some-repo/pages"
        assert headers["Authorization"] == f"Bearer {github_tools.settings.github_pat}"
        assert json == {"source": {"branch": "main", "path": "/"}}
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = github_tools.enable_github_pages(repo="some-repo", confirm_write=True)
    assert out["status"] == "success"
    assert out["org"] == "TrueSightDAO"
    assert out["url"] == "https://truesightdao.github.io/some-repo/"
    assert out["status_field"] == "building"


def test_enable_github_pages_tool_spec_registered():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("enable_github_pages")
    assert spec is not None
