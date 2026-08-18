"""Unit tests for the allowed_repos/api_only_repos gate on upload_file_to_github."""

from __future__ import annotations

import httpx

from app.config import settings
from app.tools import upload_file_to_github as uftg


def test_refuses_repo_outside_allowed_and_api_only(monkeypatch):
    monkeypatch.setattr(settings, "allowed_repos", ["truesight_autopilot"])
    monkeypatch.setattr(settings, "api_only_repos", ["truesight_autopilot_transcript"])
    result = uftg.upload_file_to_github(
        repo="some_other_repo", path="x.txt", content="hi"
    )
    assert result["status"] == "error"
    assert "some_other_repo" in result["error"]


def test_allows_repo_in_api_only_repos(monkeypatch):
    """Repo is in api_only_repos (not allowed_repos) — gate passes, request proceeds."""
    monkeypatch.setattr(settings, "allowed_repos", [])
    monkeypatch.setattr(settings, "api_only_repos", ["bionpact_attachments"])

    def fake_get(*args, **kwargs):
        return httpx.Response(404, json={}, request=httpx.Request("GET", args[0]))

    def fake_put(*args, **kwargs):
        return httpx.Response(
            201,
            json={
                "commit": {"sha": "abc123"},
                "content": {"html_url": "https://example.com"},
            },
            request=httpx.Request("PUT", args[0]),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)

    result = uftg.upload_file_to_github(
        repo="bionpact_attachments", path="x.txt", content="hi"
    )
    assert result["status"] == "success"


def test_allows_repo_in_allowed_repos(monkeypatch):
    """Repo is in allowed_repos (not api_only_repos) — gate passes too."""
    monkeypatch.setattr(settings, "allowed_repos", ["bionpact_agentic_ai_context"])
    monkeypatch.setattr(settings, "api_only_repos", [])

    def fake_get(*args, **kwargs):
        return httpx.Response(404, json={}, request=httpx.Request("GET", args[0]))

    def fake_put(*args, **kwargs):
        return httpx.Response(
            201,
            json={
                "commit": {"sha": "abc123"},
                "content": {"html_url": "https://example.com"},
            },
            request=httpx.Request("PUT", args[0]),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)

    result = uftg.upload_file_to_github(
        repo="bionpact_agentic_ai_context", path="x.txt", content="hi"
    )
    assert result["status"] == "success"
