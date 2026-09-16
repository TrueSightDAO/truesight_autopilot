"""Transport tests for the asserted author tier (brain tier-awareness PR1).

The adapter asserts a sender's tier (governor / member / guest) about a chat
turn. It must ride a *signed* credential (the JWT) so only a holder of the
shared jwt_secret -- the adapters -- can set it; a plain header would be
spoofable. These tests pin that contract plus backwards compatibility.
"""

from __future__ import annotations

from app.auth import (
    AUTHOR_ROLES,
    DEFAULT_AUTHOR_ROLE,
    author_role_from_claims,
    create_jwt,
    verify_jwt_claims,
)


class _FakeRequest:
    def __init__(self, token: str | None = None, cookie: str | None = None):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.cookies = {"governor_chat_session": cookie} if cookie else {}


def test_create_jwt_defaults_to_governor_claim():
    token = create_jwt("PUBKEY_A")
    claims = verify_jwt_claims(_FakeRequest(token=token))
    assert claims["sub"] == "PUBKEY_A"
    assert claims["author_role"] == "governor"


def test_create_jwt_carries_explicit_member_role():
    token = create_jwt("PUBKEY_B", "member")
    claims = verify_jwt_claims(_FakeRequest(token=token))
    assert claims["author_role"] == "member"
    assert author_role_from_claims(claims) == "member"


def test_create_jwt_coerces_unknown_role_to_default():
    # Junk/garbage must NOT become an accidental privilege change.
    token = create_jwt("PUBKEY_C", "superadmin")
    claims = verify_jwt_claims(_FakeRequest(token=token))
    assert claims["author_role"] == DEFAULT_AUTHOR_ROLE


def test_author_role_from_claims_defaults_when_absent():
    # Tokens minted before tier-awareness carry no author_role; every such
    # caller today is a governor, so absent -> governor.
    assert author_role_from_claims({"sub": "X"}) == "governor"
    assert author_role_from_claims(None) == "governor"
    assert author_role_from_claims({}) == "governor"


def test_author_role_from_claims_rejects_tampered_value():
    assert author_role_from_claims({"author_role": "root"}) == DEFAULT_AUTHOR_ROLE


def test_role_claim_is_signed_and_not_spoofable_by_claim_editing():
    # Re-encoding a payload with a different role but no valid signature must
    # fail verification -> the role can only be set by a jwt_secret holder.
    from jose import jwt as _jwt

    from app.config import settings

    forged = _jwt.encode(
        {"sub": "PUBKEY_D", "author_role": "guest"},
        "not-the-real-secret",
        algorithm=settings.jwt_algorithm,
    )
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        verify_jwt_claims(_FakeRequest(token=forged))


def test_verify_jwt_still_returns_public_key_only():
    from app.auth import verify_jwt

    token = create_jwt("PUBKEY_E", "member")
    assert verify_jwt(_FakeRequest(token=token)) == "PUBKEY_E"


def test_author_roles_constant_is_the_tier_enum():
    # D4 (PR2) added 'sentinel' -- the DAO's AI-agent contributor tier: a
    # distinct identity class carrying governor-tier WRITE/ADMIN rights.
    assert set(AUTHOR_ROLES) == {"governor", "sentinel", "member", "guest"}
