"""Unit tests for app/governor_registry.py -- content-addressed key point-lookup.

PR3 of PUBLIC_KEY_LOOKUP_CACHE_PLAN. resolve_key() is the fast sign-in path used
by the vault and the auth gate, so its contract is pinned here against the
raw-CDN design (no GitHub contents API):

- hit             : an ACTIVE key resolves to an identity dict with the governor
                    flag derived from roles, and email withheld (privacy)
- miss            : a 404 resolves to None and is cached as a miss
- non-ACTIVE      : status other than ACTIVE resolves to None (revoked keys)
- cache-hit       : a second lookup inside the TTL does not re-fetch
- force-fresh     : is_governor() retries once on a miss, to absorb the ~5-min
                    raw.githubusercontent.com CDN lag for a freshly-registered key
- monolith-fallbk : is_governor() falls back to load_governors() enumeration
                    when the per-key surface has no answer
"""

from __future__ import annotations

import httpx
import pytest

from app import governor_registry as gr

_KEY = "-----BEGIN PUBLIC KEY-----\nFAKEKEY\n-----END PUBLIC KEY-----"


def _resp(status: int, payload: dict) -> httpx.Response:
    """Build an httpx.Response with a bound request (required by raise_for_status)."""
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "https://example.test")
    )


class FakeHTTP:
    """Scripted stand-in for httpx.get, routed by URL substring.

    Each route holds an ordered list of responses; successive calls to the same
    route advance through it and the final response sticks. Unrouted URLs 404,
    so a test only scripts the surfaces it cares about.
    """

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


@pytest.fixture(autouse=True)
def _clean_caches():
    """Isolate the per-key cache and the monolith cache between tests."""
    gr.clear_per_key_cache()
    gr._cache.update({"data": None, "fetched_at": 0.0, "url": None})
    yield
    gr.clear_per_key_cache()
    gr._cache.update({"data": None, "fetched_at": 0.0, "url": None})


def _key_url(key: str) -> str:
    return f"/public_keys/{gr._sha256(key)}.json"


def test_resolve_key_hit_returns_governor_identity(monkeypatch):
    fake = FakeHTTP().route(
        "/public_keys/",
        _resp(
            200, {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"}
        ),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = gr.resolve_key(_KEY)

    assert identity == {
        "name": "Gary Teh",
        "is_governor": True,
        "email": "",
        "roles": ["governor"],
    }
    assert len(fake.calls) == 1
    assert _key_url(_KEY) in fake.calls[0]


def test_resolve_key_hit_non_governor_role(monkeypatch):
    fake = FakeHTTP().route(
        "/public_keys/",
        _resp(200, {"contributor": "Alice", "roles": ["member"], "status": "ACTIVE"}),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = gr.resolve_key(_KEY)

    assert identity["is_governor"] is False
    assert identity["name"] == "Alice"


def test_resolve_key_miss_returns_none(monkeypatch):
    fake = FakeHTTP()  # nothing routed -> 404
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.resolve_key(_KEY) is None
    # the miss is cached (short TTL) so retries stay cheap
    assert gr._per_key_cache[gr._sha256(_KEY)][1] is None


def test_resolve_key_non_active_returns_none(monkeypatch):
    fake = FakeHTTP().route(
        "/public_keys/",
        _resp(200, {"contributor": "Gary", "roles": ["governor"], "status": "REVOKED"}),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.resolve_key(_KEY) is None


def test_resolve_key_cache_hit_skips_refetch(monkeypatch):
    fake = FakeHTTP().route(
        "/public_keys/",
        _resp(200, {"contributor": "Gary", "roles": ["governor"], "status": "ACTIVE"}),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    first = gr.resolve_key(_KEY)
    second = gr.resolve_key(_KEY)

    assert first == second
    assert len(fake.calls) == 1  # the second lookup came from the per-key cache


def test_is_governor_force_fresh_retry(monkeypatch):
    fake = FakeHTTP().route(
        "/public_keys/",
        _resp(404, {}),  # first lookup: stale CDN miss
        _resp(200, {"contributor": "Gary", "roles": ["governor"], "status": "ACTIVE"}),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.is_governor(_KEY) is True
    per_key = [c for c in fake.calls if "/public_keys/" in c]
    assert len(per_key) == 2  # initial miss + force-fresh retry


def test_is_governor_monolith_fallback(monkeypatch):
    monkeypatch.setenv("GOVERNOR_NAMES", "Gary Teh")
    fake = (
        FakeHTTP()
        .route(
            "/public_keys/",
            _resp(404, {}),  # per-key surface never answers
        )
        .route(
            "dao_members.json",
            _resp(
                200,
                {
                    "generated_at": "2026-01-01",
                    "contributors": [
                        {
                            "name": "Gary Teh",
                            "email": "gary@truesight.me",
                            "public_keys": [{"public_key": _KEY, "status": "ACTIVE"}],
                        }
                    ],
                },
            ),
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.is_governor(_KEY) is True
    # the enumeration path was actually consulted
    assert any("dao_members.json" in c for c in fake.calls)


def test_is_governor_unknown_key_is_false(monkeypatch):
    monkeypatch.setenv("GOVERNOR_NAMES", "Gary Teh")
    fake = (
        FakeHTTP()
        .route(
            "/public_keys/",
            _resp(404, {}),
        )
        .route(
            "dao_members.json",
            _resp(200, {"generated_at": "2026-01-01", "contributors": []}),
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.is_governor(_KEY) is False


# ── resolve_key_fresh: authenticated contents-API path (PR4) ─────────────────

_CONTENTS = "contents/public_keys"
_RAW = "raw.githubusercontent.com"


def test_resolve_key_fresh_reads_contents_api(monkeypatch):
    """The fresh resolver hits api.github.com (contents API), not the raw CDN."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    fake = FakeHTTP().route(
        _CONTENTS,
        _resp(
            200, {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"}
        ),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = gr.resolve_key_fresh(_KEY)

    assert identity == {
        "name": "Gary Teh",
        "is_governor": True,
        "email": "",
        "roles": ["governor"],
    }
    assert "api.github.com" in fake.calls[0]


def test_resolve_key_fresh_bypasses_stale_cache(monkeypatch):
    """A key cached as a miss is re-resolved fresh (U2: recognised immediately)."""
    monkeypatch.setenv("GITHUB_READ_PAT", "tok")
    gr._per_key_cache[gr._sha256(_KEY)] = (gr._now(), None)  # warm stale miss
    fake = FakeHTTP().route(
        _CONTENTS,
        _resp(
            200, {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"}
        ),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = gr.resolve_key_fresh(_KEY)

    assert identity is not None and identity["is_governor"] is True


def test_resolve_key_fresh_falls_back_to_raw_on_api_miss(monkeypatch):
    """No answer from the contents API -> fall back to the raw per-key file."""
    fake = (
        FakeHTTP()
        .route(_CONTENTS, _resp(404, {}))
        .route(
            _RAW,
            _resp(
                200,
                {"contributor": "Gary Teh", "roles": ["governor"], "status": "ACTIVE"},
            ),
        )
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    identity = gr.resolve_key_fresh(_KEY)

    assert identity is not None and identity["is_governor"] is True
    assert any(_RAW in c for c in fake.calls)


def test_resolve_key_fresh_revoked_returns_none(monkeypatch):
    fake = FakeHTTP().route(
        _CONTENTS,
        _resp(200, {"contributor": "Gary", "roles": ["governor"], "status": "REVOKED"}),
    )
    monkeypatch.setattr(gr.httpx, "get", fake)

    assert gr.resolve_key_fresh(_KEY) is None
