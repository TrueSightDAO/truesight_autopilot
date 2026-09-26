"""Regression tests: append_to_transcript must fail loudly (not 422) when the
pre-read is rate-limited, and must prefer the dedicated transcript PAT.

Filed 2026-09-26 (thread 780): a GitHub Contents GET returning 403
("API rate limit exceeded") left sha=None, so the script PUT without a sha and
GitHub replied with a confusing 422 "sha wasn't supplied", masking the real
cause.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "append_to_transcript.py"
    spec = importlib.util.spec_from_file_location(
        "append_to_transcript_under_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_response(status_code, headers=None, text="", payload=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.headers = headers or {}
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    return _Resp()


def test_rate_limited_status_is_surfaced_distinctly(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "get_github_token", lambda: "tok")

    fake = _fake_response(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790411438"},
        text='{"message": "API rate limit exceeded for user ID 1079482."}',
    )

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return fake

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client())
    result = mod.github_request("GET", "https://api.github.com/x")
    assert result["status"] == "rate_limited"
    assert result["reset"] == "1790411438"


def test_append_fails_loudly_when_pre_read_rate_limited(monkeypatch):
    mod = _load_script_module()

    def fake_request(method, url, data=None):
        return {"status": "rate_limited", "message": "rate limit", "reset": "1"}

    monkeypatch.setattr(mod, "github_request", fake_request)
    result = mod.append_to_transcript("sess-abc", "content", "f.jpg", "Image")
    assert result["status"] == "error"
    assert "pre-read failed" in result["message"]
    # Never a bare sha-less PUT: we must not claim success.
    assert "transcript_url" in result


def test_token_prefers_dedicated_transcript_pat(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("GITHUB_TRANSCRIPT_PAT", "dedicated")
    monkeypatch.setenv("TRUESIGHT_DAO_AUTOPILOT", "general")
    assert mod.get_github_token() == "dedicated"

    monkeypatch.delenv("GITHUB_TRANSCRIPT_PAT")
    assert mod.get_github_token() == "general"
