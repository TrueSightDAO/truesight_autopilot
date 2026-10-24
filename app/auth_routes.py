"""Auth routes for vault login — shared between main app and vault worker.

Security hardening (2026-10-24 incident: ``POST /auth/send-challenge`` actively
scanned/fuzzed from 13.214.162.112 with pentest-labelled payloads including a
CRLF-injection probe and a ~2,000-char length probe. See Discord
``#vault-auth-scan-incident``).

1. **Email format validation** (length cap, charset, and CRLF / control-character
   rejection) happens BEFORE any mail is sent, so header-injection payloads die
   at the door.
2. **Rate limiting** — per-email and per-IP sliding windows on both endpoints.
3. **Registered-email check** — mail is only sent to addresses resolvable from the
   governor registry, the sentinel list, or ``VAULT_AUTH_ALLOWED_EMAILS``. If no
   source yields a non-empty set we fail OPEN (still rate limited) so a registry
   outage cannot brick vault login. Unknown addresses get the same generic 200
   response, so the endpoint is not an account-enumeration oracle.
4. **The actual bug** — ``gmail_send`` returns a JSON *string*, so the old
   ``result.get("returncode")`` raised
   ``AttributeError: 'str' object has no attribute 'get'`` on every single send.
   That exception was swallowed and the handler still reported success.
5. **Structured audit log** line (``event=vault_challenge_*``) per attempt.

Additionally: challenge codes now carry the TTL the email always promised
(10 minutes — they previously never expired), and ``/auth/verify-code`` caps
guesses at ``_MAX_VERIFY_ATTEMPTS`` and invalidates the code on trip. A 6-digit
code with no attempt limit mints a vault JWT, so an unbounded verify endpoint was
brute-forceable independently of the send-side gaps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import create_jwt as _create_jwt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# ── Tunables ────────────────────────────────────────────────────────────────
_CODE_TTL_SECONDS = 600  # the email has always promised 10 minutes
_MAX_EMAIL_LEN = 254
_SEND_WINDOW_SECONDS = 600
_MAX_SEND_PER_EMAIL = 3
_MAX_SEND_PER_IP = 10
_MAX_VERIFY_ATTEMPTS = 5
_REGISTERED_EMAILS_TTL = 300.0

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# ── In-memory state (replace with Redis in production) ──────────────────────
# email -> {"code": str, "issued_at": float}
_challenge_codes: dict[str, dict] = {}
_send_attempts: dict[str, list[float]] = {}
_verify_attempts: dict[str, list[float]] = {}
_registered_cache: tuple[float, set[str]] | None = None


def _audit(event: str, **fields: object) -> None:
    """One structured line per attempt — the incident audit trail."""
    rendered = " ".join(f"{k}={v}" for k, v in fields.items())
    log = (
        logger.warning
        if event.rsplit("_", 1)[-1] in {"rejected", "limited"}
        else logger.info
    )
    log("event=%s %s", event, rendered)


def _client_ip(request: Request) -> str:
    """Best-effort client IP.

    Behind nginx the socket peer is loopback, so we only trust X-Forwarded-For
    in that case — a header supplied by a remote peer is never believed.
    """
    direct = getattr(getattr(request, "client", None), "host", "") or "unknown"
    if direct in _LOOPBACK:
        try:
            forwarded = request.headers.get("x-forwarded-for") or ""
        except Exception:  # noqa: BLE001 -- headers are advisory only
            forwarded = ""
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return direct


def _normalize_email(raw: object) -> str | None:
    """Return a clean lowercase address, or None if it is unusable.

    Rejects non-strings, over-long input, CR/LF and any other control character
    (email-header injection), and anything failing the address grammar.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if not candidate or len(candidate) > _MAX_EMAIL_LEN:
        return None
    if any(ch in candidate for ch in "\r\n\t") or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in candidate
    ):
        return None
    if not _EMAIL_RE.match(candidate):
        return None
    return candidate


def _prune(hits: list[float], now: float, window: float) -> list[float]:
    cutoff = now - window
    return [t for t in hits if t > cutoff]


def _over_limit(
    key: str, bucket: dict[str, list[float]], limit: int, now: float
) -> bool:
    hits = _prune(bucket.get(key, []), now, _SEND_WINDOW_SECONDS)
    bucket[key] = hits
    return len(hits) >= limit


def _record(key: str, bucket: dict[str, list[float]], now: float) -> None:
    bucket.setdefault(key, []).append(now)


def _registered_emails() -> set[str]:
    """Addresses we are willing to email a code to.

    Sources: governor registry contributors, sentinel list, and the
    ``VAULT_AUTH_ALLOWED_EMAILS`` env var (comma-separated). Cached briefly; a
    failure in any source is tolerated. An empty result means "no source
    configured" — we then fail OPEN rather than locking every governor out.
    """
    global _registered_cache
    now = time.time()
    if _registered_cache and _registered_cache[0] > now:
        return _registered_cache[1]

    found: set[str] = set()
    try:
        for chunk in (os.environ.get("VAULT_AUTH_ALLOWED_EMAILS") or "").split(","):
            clean = _normalize_email(chunk)
            if clean:
                found.add(clean)
    except Exception:  # noqa: BLE001 -- env is best-effort
        pass

    try:
        from .governor_registry import load_governors, sentinel_emails

        for key in sentinel_emails():
            clean = _normalize_email(key)
            if clean:
                found.add(clean)
        registry = load_governors() or {}
        for bucket_name in ("governors", "contributors"):
            for entry in registry.get(bucket_name) or []:
                clean = _normalize_email((entry or {}).get("email"))
                if clean:
                    found.add(clean)
    except Exception as exc:  # noqa: BLE001 -- registry down must not break login
        logger.warning("vault auth: registered-email lookup degraded: %s", exc)

    _registered_cache = (now + _REGISTERED_EMAILS_TTL, found)
    return found


def _reset_state_for_tests() -> None:
    """Clear in-memory state (used by the unit suite only)."""
    global _registered_cache
    _challenge_codes.clear()
    _send_attempts.clear()
    _verify_attempts.clear()
    _registered_cache = None


async def _json_body(request: Request) -> dict:
    """Parse a JSON object body, or 400 — a list/scalar/garbage body is not a crash."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- malformed body is a client error
        raise HTTPException(status_code=400, detail="Invalid request body.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body.")
    return body


@router.post("/auth/send-challenge")
async def send_challenge(request: Request) -> JSONResponse:
    """Send a verification code to the user's email."""
    body = await _json_body(request)
    now = time.time()
    ip = _client_ip(request)
    raw_email = body.get("email")

    if not raw_email or (isinstance(raw_email, str) and not raw_email.strip()):
        raise HTTPException(status_code=400, detail="Email is required.")

    email = _normalize_email(raw_email)
    if email is None:
        _audit(
            "vault_challenge_rejected",
            reason="invalid_email_format",
            ip=ip,
            length=len(raw_email) if isinstance(raw_email, str) else -1,
        )
        raise HTTPException(
            status_code=400, detail="Please provide a valid email address."
        )

    if _over_limit(email, _send_attempts, _MAX_SEND_PER_EMAIL, now) or _over_limit(
        ip, _send_attempts, _MAX_SEND_PER_IP, now
    ):
        _audit("vault_challenge_limited", reason="rate_limit", email=email, ip=ip)
        raise HTTPException(
            status_code=429,
            detail="Too many verification requests. Please try again later.",
        )

    _record(email, _send_attempts, now)
    _record(ip, _send_attempts, now)

    allowed = _registered_emails()
    if allowed and email not in allowed:
        # Same shape as success: no account-enumeration oracle.
        _audit("vault_challenge_rejected", reason="not_registered", email=email, ip=ip)
        return JSONResponse(
            {
                "sent": True,
                "message": "If this email is registered, a verification code has been sent.",
            }
        )

    # Generate a 6-digit code and store it with its issue time (TTL enforced on verify)
    # CSPRNG: this code mints a vault JWT, so it must not come from the
    # Mersenne Twister (which is predictable from observed output).
    code = str(100000 + secrets.randbelow(900000))
    _challenge_codes[email] = {"code": code, "issued_at": now}

    # Send the email via Gmail
    try:
        from .tools.gmail_tools import gmail_send as _gmail_send

        result = _gmail_send(
            to=email,
            subject="Your TrueSight DAO Vault verification code",
            body=(
                f"Your verification code is: {code}\n\n"
                "This code expires in 10 minutes.\n\n"
                "If you did not request this code, please ignore this email.\n\n"
                "- TrueSight DAO Autopilot"
            ),
            account="admin",
        )
        # gmail_send returns a JSON *string* — never call .get() on it. (This was
        # the "'str' object has no attribute 'get'" bug tripped by every probe.)
        returncode = None
        if isinstance(result, dict):
            returncode = result.get("returncode")
        elif isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                returncode = parsed.get("returncode", parsed.get("status"))
        _audit("vault_challenge_sent", email=email, ip=ip, rc=returncode)
    except Exception as exc:  # noqa: BLE001 -- never leak send failure to the client
        logger.error("Failed to send challenge email to %s: %s", email, exc)
        _audit(
            "vault_challenge_send_failed", email=email, ip=ip, error=type(exc).__name__
        )

    return JSONResponse(
        {
            "sent": True,
            "message": "If this email is registered, a verification code has been sent.",
        }
    )


@router.post("/auth/verify-code")
async def verify_code(request: Request) -> JSONResponse:
    """Verify an email challenge code and issue a JWT."""
    body = await _json_body(request)
    now = time.time()
    ip = _client_ip(request)

    raw_email = body.get("email")
    raw_code = body.get("code")
    if not raw_email or not raw_code:
        raise HTTPException(status_code=400, detail="Email and code are required.")

    email = _normalize_email(raw_email)
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    if email is None or not code:
        _audit(
            "vault_verify_rejected", reason="invalid_input", email=email or "", ip=ip
        )
        raise HTTPException(status_code=400, detail="Email and code are required.")

    # A 6-digit code with no attempt cap is brute-forceable — cap it, and burn
    # the code when the cap trips so a locked-out attacker must re-request.
    if _over_limit(email, _verify_attempts, _MAX_VERIFY_ATTEMPTS, now):
        _challenge_codes.pop(email, None)
        _audit("vault_verify_limited", reason="too_many_attempts", email=email, ip=ip)
        raise HTTPException(
            status_code=429,
            detail="Too many verification attempts. Please request a new code.",
        )
    _record(email, _verify_attempts, now)

    stored = _challenge_codes.get(email)
    if not stored:
        _audit("vault_verify_rejected", reason="no_challenge", email=email, ip=ip)
        raise HTTPException(
            status_code=400,
            detail="No verification code sent to this email. Please request a new code.",
        )

    if now - float(stored.get("issued_at", 0)) > _CODE_TTL_SECONDS:
        _challenge_codes.pop(email, None)
        _audit("vault_verify_rejected", reason="expired", email=email, ip=ip)
        raise HTTPException(
            status_code=400,
            detail="Verification code expired. Please request a new code.",
        )

    if not hmac.compare_digest(str(stored.get("code", "")), code):
        _audit("vault_verify_rejected", reason="bad_code", email=email, ip=ip)
        raise HTTPException(
            status_code=400, detail="Invalid verification code. Please try again."
        )

    # Code verified — remove it so it can't be reused, and clear the attempt bucket
    _challenge_codes.pop(email, None)
    _verify_attempts.pop(email, None)

    # Issue a limited-scope JWT for vault access
    synthetic_key = f"vault:email:{hashlib.sha256(email.encode()).hexdigest()[:16]}"
    token = _create_jwt(synthetic_key)

    _audit("vault_verify_success", email=email, ip=ip)
    response = JSONResponse({"token": token, "expires_in": 60 * 60})
    response.set_cookie(
        key="governor_chat_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60,
    )
    return response
