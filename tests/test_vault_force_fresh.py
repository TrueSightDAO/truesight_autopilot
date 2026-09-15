"""Unit tests for the vault sign-in force-fresh-on-deny path (PR4).

_resolve_identity_for_signin() must grant a governor whose per-key file was
written moments ago but is not yet visible via the raw CDN — the 2026-06-16
cache-lag bug. It does one fresh contents-API lookup before refusing, and ONLY
on the sign-in path (page renders keep using the fast path).
"""

from __future__ import annotations

import httpx

from app import governor_registry as gr
from app import vault_routes as vr

_KEY = "-----BEGIN PUBLIC KEY-----\nFAKEKEY\n-----END PUBLIC KEY-----"


def _resp(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "https://example.test")
    )


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._script: dict[str, list[httpx.Response]] = {}
        self._idx: dict[str, int] = {}

    def route(self, substring: str, *responses: httpx.Response) -> "FakeHTTP":
        self._script[substring] = list(responses)
        return self

    def __call__(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append(url)
        for substring, seq in self._script.items():
            if substring in url:
                i = self._idx.get(substring, 0)
                self._idx[substring] = i + 1
                return seq[min(i, len(seq) - 1)]
        return _resp(404, {})


def _clean():
    gr.clear_per_key_cache()
    gr._cache.update({"data": None, "fetched_at": 0.0, "url": None})


def test_signin_grants_fresh_key_via_contents_api(monkeypatch):
    """U2: raw CDN still 404s, but the contents API has the key -> GRANTED."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    monkeypatch.setenv("GOVERNOR_NAMES", "Gary Teh")
    _clean()
    fake = (
        FakeHTTP()
        # fast path (resolve_key -> raw) misses; monolith fallback also empty
        .route(
            "raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys",
            _resp(404, {}),
        )
        .route("dao_members.json", _resp(200, {"contributors": []}))
        # force-fresh path (resolve_key_fresh -> contents API) says ACTIVE
        .route(
            "api.github.com/repos/TrueSightDAO/treasury-cache/contents",
            _resp(
                200,
                {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"},
            ),
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = vr._resolve_identity_for_signin(_KEY)

    assert identity["is_governor"] is True
    assert identity["name"] == "Gary Teh"
    assert any("api.github.com" in c for c in fake.calls)


def test_signin_denies_when_truly_unknown(monkeypatch):
    """A genuinely unknown key is denied; the fresh lookup was still attempted."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    monkeypatch.setenv("GOVERNOR_NAMES", "Gary Teh")
    _clean()
    fake = (
        FakeHTTP()
        .route(
            "raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys",
            _resp(404, {}),
        )
        .route("dao_members.json", _resp(200, {"contributors": []}))
        .route(
            "api.github.com/repos/TrueSightDAO/treasury-cache/contents", _resp(404, {})
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = vr._resolve_identity_for_signin(_KEY)

    assert identity["is_governor"] is False
    assert any("api.github.com" in c for c in fake.calls)  # fresh lookup happened


def test_signin_known_governor_short_circuits(monkeypatch):
    """A warm-cache governor is granted WITHOUT touching the contents API."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    _clean()
    fake = FakeHTTP().route(
        "raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys",
        _resp(
            200, {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"}
        ),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = vr._resolve_identity_for_signin(_KEY)

    assert identity["is_governor"] is True
    assert not any("api.github.com" in c for c in fake.calls)


def test_signin_fresh_lookup_does_not_grant_non_governor(monkeypatch):
    """A fresh ACTIVE member (no governor role) is still denied vault access."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    _clean()
    fake = (
        FakeHTTP()
        .route(
            "raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys",
            _resp(404, {}),
        )
        .route("dao_members.json", _resp(200, {"contributors": []}))
        .route(
            "api.github.com/repos/TrueSightDAO/treasury-cache/contents",
            _resp(
                200, {"contributor": "Alice", "roles": ["member"], "status": "ACTIVE"}
            ),
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = vr._resolve_identity_for_signin(_KEY)

    assert identity["is_governor"] is False
