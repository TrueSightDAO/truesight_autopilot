"""Unit tests for the repo-access audit log (SOPHIA_REPO_ACCESS_DENYLIST_PLAN PR4).

The audit writer must be:
- ENABLED (record written + feed prepended) when a PAT exists and we force it on,
- FAIL-SOFT (never raise, never block the write it audits) on any error,
- DISABLED (no network at all) under a hermetic pytest run with no force flag.
"""

from __future__ import annotations

import base64
import json

import httpx

from app import repo_access_audit as raa


class _Recorder:
    """Captures GET/PUT calls and returns scripted responses."""

    def __init__(self):
        self.gets: list[str] = []
        self.puts: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))

    def put(self, url, json=None, **kwargs):  # noqa: A002 - httpx kwarg name
        self.puts.append((url, json))
        return httpx.Response(
            201,
            json={
                "commit": {"sha": "deadbeef"},
                "content": {"html_url": "https://example.com/c"},
            },
            request=httpx.Request("PUT", url),
        )


def test_records_when_forced(monkeypatch):
    monkeypatch.setattr(raa.settings, "github_pat", "test-pat")
    monkeypatch.setattr(raa.settings, "strict_repos", ["truesight_autopilot"])
    monkeypatch.setenv("REPO_ACCESS_AUDIT_FORCE", "1")
    rec = _Recorder()
    monkeypatch.setattr(raa.httpx, "get", rec.get)
    monkeypatch.setattr(raa.httpx, "put", rec.put)

    res = raa.record_repo_access(
        repo="truesight_autopilot",
        action="git_push_changes",
        result="success",
        evidence_url="https://github.com/TrueSightDAO/truesight_autopilot/pull/1",
    )
    assert res["status"] == "success"
    # one record file + one feed update
    assert len(rec.puts) == 2
    paths = [u for u, _ in rec.puts]
    assert any("/repo_access/repo_access_" in p for p in paths)
    assert any(p.endswith("/repo_access/manifest.json") for p in paths)

    # the record body is a valid JSON record tagged strict-mode
    rec_path, rec_payload = next(
        (u, j) for u, j in rec.puts if "/repo_access/repo_access_" in u
    )
    body = json.loads(base64.b64decode(rec_payload["content"]).decode())
    assert body["repo"] == "truesight_autopilot"
    assert body["action"] == "git_push_changes"
    assert body["mode"] == "strict"
    assert body["result"] == "success"


def test_mode_default_allow(monkeypatch):
    """A repo NOT in strict_repos is tagged default-allow."""
    monkeypatch.setattr(raa.settings, "github_pat", "test-pat")
    monkeypatch.setattr(raa.settings, "strict_repos", [])
    monkeypatch.setenv("REPO_ACCESS_AUDIT_FORCE", "1")
    rec = _Recorder()
    monkeypatch.setattr(raa.httpx, "get", rec.get)
    monkeypatch.setattr(raa.httpx, "put", rec.put)

    res = raa.record_repo_access(repo="some_new_repo", action="create_repo")
    assert res["status"] == "success"
    _, rec_payload = next(
        (u, j) for u, j in rec.puts if "/repo_access/repo_access_" in u
    )
    body = json.loads(base64.b64decode(rec_payload["content"]).decode())
    assert body["mode"] == "default-allow"


def test_fail_soft_on_error(monkeypatch):
    """An HTTP blow-up returns an error dict and never raises."""
    monkeypatch.setattr(raa.settings, "github_pat", "test-pat")
    monkeypatch.setenv("REPO_ACCESS_AUDIT_FORCE", "1")

    def boom(*a, **k):
        raise httpx.RequestError("network down")

    monkeypatch.setattr(raa.httpx, "get", boom)
    monkeypatch.setattr(raa.httpx, "put", boom)

    res = raa.record_repo_access(repo="truesight_autopilot", action="merge_pr")
    assert res["status"] == "error"
    assert "error" in res


def test_disabled_under_pytest(monkeypatch):
    """No PAT => skipped, and zero HTTP calls."""
    monkeypatch.setattr(raa.settings, "github_pat", "")
    monkeypatch.delenv("REPO_ACCESS_AUDIT_FORCE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return httpx.Response(200, json={}, request=httpx.Request("GET", a[0]))

    monkeypatch.setattr(raa.httpx, "get", spy)
    monkeypatch.setattr(raa.httpx, "put", spy)
    res = raa.record_repo_access(repo="truesight_autopilot", action="merge_pr")
    assert res["status"] == "skipped"
    assert calls["n"] == 0


def test_hermetic_pytest_skips_without_force(monkeypatch):
    """A PAT present but PYTEST_CURRENT_TEST set and no force => skipped."""
    monkeypatch.setattr(raa.settings, "github_pat", "test-pat")
    monkeypatch.delenv("REPO_ACCESS_AUDIT_FORCE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    res = raa.record_repo_access(repo="truesight_autopilot", action="merge_pr")
    assert res["status"] == "skipped"
