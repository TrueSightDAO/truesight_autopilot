"""Authentication layer: RSA signature verification + JWT sessions."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

from .config import settings
from .governor_registry import is_governor as _is_governor_from_registry

# In-memory nonce cache (replace with Redis in production)
_seen_nonces: set[str] = set()


def _base64_to_bytes(b64: str) -> bytes:
    normalized = b64.replace("\r", "").replace("\n", "").replace("-", "+").replace("_", "/")
    padded = normalized + "==="[: (4 - len(normalized) % 4) % 4]
    return base64.b64decode(padded)


def _import_spki_key(b64_spki: str):
    """Import an RSA SPKI public key for verification using cryptography."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    der = _base64_to_bytes(b64_spki)
    public_key = serialization.load_der_public_key(der, backend=default_backend())
    return public_key


def verify_rsa_signature(payload_json: str, signature_b64: str, public_key_b64: str) -> bool:
    """Verify an RSA-PKCS1-v1_5 / SHA-256 signature against a base64 SPKI public key."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = _import_spki_key(public_key_b64)
        signature = _base64_to_bytes(signature_b64)
        public_key.verify(
            signature,
            payload_json.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def verify_payload(payload: dict, signature: str, public_key_b64: str) -> None:
    """Full verification: signature + timestamp + nonce + governor status."""
    # 1. Signature
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if not verify_rsa_signature(payload_json, signature, public_key_b64):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature.",
        )

    # 2. Timestamp skew
    ts_str = payload.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid timestamp format.",
        ) from None
    now = datetime.now(timezone.utc)
    skew = abs((now - ts).total_seconds())
    if skew > settings.timestamp_skew_seconds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Timestamp skew too large ({int(skew)}s).",
        )

    # 3. Nonce replay
    nonce = payload.get("nonce", "")
    if not nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nonce is required.",
        )
    if nonce in _seen_nonces:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nonce already used (replay detected).",
        )
    _seen_nonces.add(nonce)

    # 4. Governor check
    if not settings.disable_governor_check and not is_governor(public_key_b64):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to authorized governors.",
        )


def is_governor(public_key_b64: str) -> bool:
    """Check if a public key belongs to a registered governor."""
    return _is_governor_from_registry(public_key_b64)


def create_jwt(public_key_b64: str) -> str:
    """Issue a short-lived JWT for session continuity."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": public_key_b64,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
        "jti": str(uuid.uuid4()),
        "scope": "governor_chat",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_jwt(request: Request) -> str:
    """Extract and verify JWT from Authorization header or cookie. Returns public_key."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    else:
        token = request.cookies.get("governor_chat_session", "")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        public_key = payload.get("sub")
        if not public_key:
            raise JWTError("No subject in token")
        return public_key
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
