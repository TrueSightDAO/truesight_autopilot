"""Auth routes for vault login — shared between main app and vault worker."""

from __future__ import annotations

import hashlib
import logging
import random

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import create_jwt as _create_jwt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# In-memory challenge code store (replace with Redis in production)
_challenge_codes: dict[str, str] = {}


@router.post("/auth/send-challenge")
async def send_challenge(request: Request) -> JSONResponse:
    """Send a verification code to the user's email."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")

    # Generate a 6-digit code
    code = str(random.randint(100000, 999999))

    # Store the code in memory
    _challenge_codes[email] = code

    # Send the email via Gmail
    try:
        from .tools.gmail_tools import gmail_send as _gmail_send

        result = _gmail_send(
            to=email,
            subject="Your TrueSight DAO Vault verification code",
            body=f"Your verification code is: {code}\n\nThis code expires in 10 minutes.\n\nIf you did not request this code, please ignore this email.\n\n- TrueSight DAO Autopilot",
            account="admin",
        )
        logger.info(
            "Challenge code sent to %s (rc=%s)", email, result.get("returncode")
        )
    except Exception as e:
        logger.error("Failed to send challenge email to %s: %s", email, e)
        # Don't reveal to the user whether sending failed (anti-phishing)
        pass

    return JSONResponse(
        {
            "sent": True,
            "message": "If this email is registered, a verification code has been sent.",
        }
    )


@router.post("/auth/verify-code")
async def verify_code(request: Request) -> JSONResponse:
    """Verify an email challenge code and issue a JWT."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()

    if not email or not code:
        raise HTTPException(status_code=400, detail="Email and code are required.")

    # Check the stored code
    stored = _challenge_codes.get(email)
    if not stored:
        raise HTTPException(
            status_code=400,
            detail="No verification code sent to this email. Please request a new code.",
        )

    if code != stored:
        raise HTTPException(
            status_code=400, detail="Invalid verification code. Please try again."
        )

    # Code verified - remove it so it can't be reused
    del _challenge_codes[email]

    # Issue a limited-scope JWT for vault access
    synthetic_key = f"vault:email:{hashlib.sha256(email.encode()).hexdigest()[:16]}"
    token = _create_jwt(synthetic_key)

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
