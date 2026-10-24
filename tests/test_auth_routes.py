"""Unit tests for the hardened vault auth routes.

Regression + abuse coverage for the 2026-10-24 ``#vault-auth-scan-incident``:
format rejection, CRLF/control-char injection, oversize payloads, rate limits,
unregistered-email silence, code TTL, brute-force cap, and the
``'str' object has no attribute 'get'`` send-path bug.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi import HTTPException

from app import auth_routes


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102 -- case-insensitive like Starlette
        return super().get(key.lower(), default)


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class FakeRequest:
    """Minimal Request stand-in: async json(), .client.host, .headers."""

    def __init__(
        self, body, host: str = "203.0.113.9", xff: str | None = None, malformed=False
    ):
        self._body = body
        self._malformed = malformed
        self.client = _FakeClient(host)
        headers = _FakeHeaders()
        if xff is not None:
            headers["x-forwarded-for"] = xff
        self.headers = headers

    async def json(self):
        if self._malformed:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    auth_routes._reset_state_for_tests()
    monkeypatch.setenv(
        "VAULT_AUTH_ALLOWED_EMAILS", "gary@truesight.me,ops@truesight.me"
    )
    monkeypatch.setattr(auth_routes, "_create_jwt", lambda key: "fake.jwt.token")
    yield
    auth_routes._reset_state_for_tests()


@pytest.fixture
def sent(monkeypatch):
    """Install a stub gmail_tools module and record calls."""
    calls: list[dict] = []
    module = types.ModuleType("app.tools.gmail_tools")

    def fake_gmail_send(**kwargs):
        calls.append(kwargs)
        return '{"status": "sent", "returncode": 0}'

    module.gmail_send = fake_gmail_send
    monkeypatch.setitem(sys.modules, "app.tools.gmail_tools", module)
    return calls


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ── format validation ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "not-an-email",
        "a@b",
        "pentest-33780-a@example",  # no TLD
        "@example.com",
        "a" * 250 + "@example.com",  # > 254 chars
        "gary@truesight.me\r\nBcc: attacker@evil.test",  # CRLF injection
        "gary@truesight.me\nBcc: attacker@evil.test",
        "gary\x00@truesight.me",
        "gary@true sight.me",
        123,
        None,
    ],
)
def test_send_challenge_rejects_bad_email(bad, sent):
    req = FakeRequest({"email": bad})
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(req))
    assert exc.value.status_code == 400
    assert sent == [], "no mail may be sent for an invalid address"


def test_send_challenge_oversize_length_probe(sent):
    """The ~2,000-char aaaa...@example.com overflow probe from the incident."""
    req = FakeRequest({"email": "a" * 2000 + "@example.com"})
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(req))
    assert exc.value.status_code == 400
    assert sent == []


def test_missing_email_is_400(sent):
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(FakeRequest({})))
    assert exc.value.status_code == 400
    assert "Email is required" in exc.value.detail
    assert sent == []


# ── malformed bodies ────────────────────────────────────────────────────────
def test_malformed_json_is_400_not_500(sent):
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(FakeRequest(None, malformed=True)))
    assert exc.value.status_code == 400
    assert sent == []


def test_non_dict_body_is_400(sent):
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(FakeRequest(["gary@truesight.me"])))
    assert exc.value.status_code == 400
    assert sent == []


# ── registered-email gate ───────────────────────────────────────────────────
def test_unregistered_email_gets_generic_200_and_no_mail(sent):
    resp = _run(
        auth_routes.send_challenge(FakeRequest({"email": "pentest-a@example.com"}))
    )
    assert resp.status_code == 200
    assert sent == []
    assert auth_routes._challenge_codes == {}


def test_registered_email_sends_and_stores_ttl(sent):
    resp = _run(auth_routes.send_challenge(FakeRequest({"email": "Gary@Truesight.me"})))
    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["to"] == "gary@truesight.me"
    stored = auth_routes._challenge_codes["gary@truesight.me"]
    assert set(stored) == {"code", "issued_at"}
    assert len(stored["code"]) == 6


def test_fails_open_when_no_allowlist_source(monkeypatch, sent):
    """A registry/allowlist outage must not brick vault login."""
    monkeypatch.delenv("VAULT_AUTH_ALLOWED_EMAILS", raising=False)
    monkeypatch.setattr(auth_routes, "_registered_emails", lambda: set())
    resp = _run(auth_routes.send_challenge(FakeRequest({"email": "someone@real.test"})))
    assert resp.status_code == 200
    assert len(sent) == 1


# ── the actual bug: gmail_send returns a JSON string ────────────────────────
def test_send_survives_json_string_result(monkeypatch):
    """Old code did result.get(...) -> AttributeError on every send."""
    module = types.ModuleType("app.tools.gmail_tools")
    module.gmail_send = lambda **kw: '{"status": "sent", "returncode": 0}'
    monkeypatch.setitem(sys.modules, "app.tools.gmail_tools", module)
    resp = _run(auth_routes.send_challenge(FakeRequest({"email": "gary@truesight.me"})))
    assert resp.status_code == 200


def test_send_survives_non_json_result(monkeypatch):
    module = types.ModuleType("app.tools.gmail_tools")
    module.gmail_send = lambda **kw: "plain text, not json"
    monkeypatch.setitem(sys.modules, "app.tools.gmail_tools", module)
    resp = _run(auth_routes.send_challenge(FakeRequest({"email": "gary@truesight.me"})))
    assert resp.status_code == 200


def test_send_failure_does_not_leak(monkeypatch):
    module = types.ModuleType("app.tools.gmail_tools")

    def boom(**kw):
        raise RuntimeError("smtp down")

    module.gmail_send = boom
    monkeypatch.setitem(sys.modules, "app.tools.gmail_tools", module)
    resp = _run(auth_routes.send_challenge(FakeRequest({"email": "gary@truesight.me"})))
    assert resp.status_code == 200
    assert resp.body.decode().find("smtp down") == -1


# ── rate limiting ───────────────────────────────────────────────────────────
def test_send_rate_limit_per_email(sent):
    for _ in range(auth_routes._MAX_SEND_PER_EMAIL):
        assert (
            _run(
                auth_routes.send_challenge(FakeRequest({"email": "gary@truesight.me"}))
            ).status_code
            == 200
        )
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(FakeRequest({"email": "gary@truesight.me"})))
    assert exc.value.status_code == 429


def test_send_rate_limit_per_ip_spoofed_xff(sent):
    """Rotating X-Forwarded-For from a remote peer must not bypass the per-IP cap."""
    last = None
    for i in range(auth_routes._MAX_SEND_PER_IP + 1):
        req = FakeRequest(
            {"email": f"u{i}@truesight.me"}, host="13.214.162.112", xff=f"10.0.0.{i}"
        )
        try:
            last = _run(auth_routes.send_challenge(req))
        except HTTPException as exc:
            last = exc
    assert isinstance(last, HTTPException) and last.status_code == 429


def test_xff_honoured_behind_loopback_proxy(sent):
    for i in range(auth_routes._MAX_SEND_PER_EMAIL):
        req = FakeRequest(
            {"email": "gary@truesight.me"}, host="127.0.0.1", xff=f"198.51.100.{i}"
        )
        assert _run(auth_routes.send_challenge(req)).status_code == 200
    req = FakeRequest(
        {"email": "gary@truesight.me"}, host="127.0.0.1", xff="198.51.100.99"
    )
    with pytest.raises(HTTPException) as exc:
        _run(auth_routes.send_challenge(req))
    assert exc.value.status_code == 429


# ── verify: TTL, brute force, success ───────────────────────────────────────
def _seed(email="gary@truesight.me", code="123456", age=0.0):
    import time

    auth_routes._challenge_codes[email] = {"code": code, "issued_at": time.time() - age}


def test_verify_expired_code_rejected(sent):
    _seed(age=auth_routes._CODE_TTL_SECONDS + 5)
    with pytest.raises(HTTPException) as exc:
        _run(
            auth_routes.verify_code(
                FakeRequest({"email": "gary@truesight.me", "code": "123456"})
            )
        )
    assert exc.value.status_code == 400
    assert "expired" in exc.value.detail.lower()
    assert "gary@truesight.me" not in auth_routes._challenge_codes


def test_verify_wrong_code_rejected(sent):
    _seed()
    with pytest.raises(HTTPException) as exc:
        _run(
            auth_routes.verify_code(
                FakeRequest({"email": "gary@truesight.me", "code": "000000"})
            )
        )
    assert exc.value.status_code == 400


def test_verify_brute_force_capped_and_code_burned(sent):
    _seed()
    for _ in range(auth_routes._MAX_VERIFY_ATTEMPTS):
        with pytest.raises(HTTPException) as exc:
            _run(
                auth_routes.verify_code(
                    FakeRequest({"email": "gary@truesight.me", "code": "000000"})
                )
            )
        assert exc.value.status_code == 400
    # next attempt is throttled and the code is withdrawn
    with pytest.raises(HTTPException) as exc:
        _run(
            auth_routes.verify_code(
                FakeRequest({"email": "gary@truesight.me", "code": "123456"})
            )
        )
    assert exc.value.status_code == 429
    assert "gary@truesight.me" not in auth_routes._challenge_codes


def test_verify_success_issues_token_and_burns_code(sent):
    _seed()
    resp = _run(
        auth_routes.verify_code(
            FakeRequest({"email": "gary@truesight.me", "code": "123456"})
        )
    )
    assert resp.status_code == 200
    assert b"fake.jwt.token" in resp.body
    assert "gary@truesight.me" not in auth_routes._challenge_codes
    # cannot replay
    with pytest.raises(HTTPException) as exc:
        _run(
            auth_routes.verify_code(
                FakeRequest({"email": "gary@truesight.me", "code": "123456"})
            )
        )
    assert exc.value.status_code == 400


def test_verify_no_challenge_rejected(sent):
    with pytest.raises(HTTPException) as exc:
        _run(
            auth_routes.verify_code(
                FakeRequest({"email": "gary@truesight.me", "code": "123456"})
            )
        )
    assert exc.value.status_code == 400
