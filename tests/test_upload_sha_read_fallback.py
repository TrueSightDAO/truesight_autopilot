"""Regression: upload_file_to_github survives a rate-limited existing-file sha read.

When api.github.com rate-limits READS (403) but writes still authenticate, the
tool must fall back to computing the git blob sha from the raw file, and retry
once on a 409/422 sha mismatch.
"""

from __future__ import annotations

import hashlib

import httpx

from app.config import settings
from app.tools import upload_file_to_github as uftg

BLOB = b'{"currencies":["Brazilian Reis"]}'


def test_raw_sha_matches_git_blob_hash(monkeypatch):
    """_raw_sha computes sha1('blob <len>\\0' + content)."""

    def fake_get(url, **kwargs):
        return httpx.Response(200, content=BLOB, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    expected = hashlib.sha1(b"blob %d\0" % len(BLOB) + BLOB).hexdigest()
    assert uftg._raw_sha("agroverse-inventory", "currencies.json", "main") == expected


def test_update_survives_rate_limited_read(monkeypatch):
    """API read 403 -> raw-sha fallback -> PUT with sha succeeds."""
    monkeypatch.setattr(settings, "strict_repos", [])
    seen = {}

    def fake_get(url, **kwargs):
        if "api.github.com" in url:  # rate-limited Contents-API read
            return httpx.Response(403, json={}, request=httpx.Request("GET", url))
        return httpx.Response(200, content=BLOB, request=httpx.Request("GET", url))

    def fake_put(url, **kwargs):
        payload = kwargs.get("json", {})
        seen["sha"] = payload.get("sha")
        return httpx.Response(
            200,
            json={
                "commit": {"sha": "abc123"},
                "content": {"html_url": "https://example.com"},
            },
            request=httpx.Request("PUT", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)

    result = uftg.upload_file_to_github(
        repo="agroverse-inventory", path="currencies.json", content="{}"
    )
    assert result["status"] == "success"
    assert result["action"] == "updated"
    assert seen["sha"] == hashlib.sha1(b"blob %d\0" % len(BLOB) + BLOB).hexdigest()


def test_retries_once_on_sha_mismatch(monkeypatch):
    """First PUT 409 (stale sha) -> re-resolve -> second PUT succeeds."""
    monkeypatch.setattr(settings, "strict_repos", [])
    monkeypatch.setattr(uftg, "_api_sha", lambda *a: "")
    monkeypatch.setattr(uftg, "_raw_sha", lambda *a: "deadbeef")
    calls = {"n": 0}

    def fake_put(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(409, json={}, request=httpx.Request("PUT", url))
        return httpx.Response(
            200,
            json={
                "commit": {"sha": "def456"},
                "content": {"html_url": "https://example.com"},
            },
            request=httpx.Request("PUT", url),
        )

    monkeypatch.setattr(httpx, "put", fake_put)

    result = uftg.upload_file_to_github(
        repo="agroverse-inventory", path="currencies.json", content="{}"
    )
    assert result["status"] == "success"
    assert calls["n"] == 2
