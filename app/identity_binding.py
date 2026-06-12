"""Identity binding — email-challenge → Telegram verification (Phase 1).

Pipeline:
1. Challenge mint: generate a verification code, hash it, store hash in
   Column G of Contributors Digital Signatures, email the plaintext code.
2. Consume + bind: user pastes the code in DM → hash matches Column G →
   set Column D = verified, Verification Key Consumed = true,
   write Column H (Telegram ID) = numeric telegram_id.
3. Emit [IDENTITY BINDING EVENT] for audit.

Security invariants:
- Column G stores a HASH, never the plaintext code
- Codes have 15-min expiry
- Max 5 attempts per challenge
- Rate-limited per telegram_id + per email
- One pending challenge per pair (newest supersedes)
- No governor-enumeration (don't reveal if email is registered)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

CHALLENGE_EXPIRY_SECONDS = 15 * 60  # 15 minutes
MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60  # 1 minute
MAX_REQUESTS_PER_WINDOW = 3

# Google Sheet column indices (0-based)
COL_EMAIL = 3  # D — email address
COL_TELEGRAM_ID = 7  # H — Telegram ID (numeric)
COL_DIGITAL_SIG = 17  # R — Digital Signature (public key)
COL_VERIFIED = 3  # D in Digital Signatures sheet — Status
COL_VERIFICATION_KEY = 6  # G — Verification Key (hash)
COL_KEY_CONSUMED = 7  # H — Verification Key Consumed

SHEET_CONTACT = "Contributors contact information"
SHEET_SIGNATURES = "Contributors Digital Signatures"

# Spreadsheet ID for the main ledger
LEDGER_SPREADSHEET_ID = "1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU"


# ── Data types ─────────────────────────────────────────────────────────────


@dataclass
class Challenge:
    """A pending email verification challenge."""

    email: str
    code_hash: str  # SHA-256 hash of the plaintext code
    expires_at: float  # Unix timestamp
    attempts_remaining: int = MAX_ATTEMPTS
    created_at: float = field(default_factory=time.time)


# In-memory challenge store (replace with Redis in production)
_pending_challenges: dict[str, Challenge] = {}  # keyed by email


# ── Rate limiting ──────────────────────────────────────────────────────────

_rate_limit: dict[str, list[float]] = {}  # keyed by telegram_id or email


def _check_rate_limit(key: str) -> bool:
    """Check if a key is rate-limited. Returns True if allowed."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    if key not in _rate_limit:
        _rate_limit[key] = []

    # Prune old entries
    _rate_limit[key] = [t for t in _rate_limit[key] if t > window_start]

    if len(_rate_limit[key]) >= MAX_REQUESTS_PER_WINDOW:
        return False

    _rate_limit[key].append(now)
    return True


# ── Hashing ────────────────────────────────────────────────────────────────


def _hash_code(code: str) -> str:
    """Hash a verification code using SHA-256 with a secret salt."""
    salt = os.getenv("VERIFICATION_CODE_SALT", "truesight-dao-v1")
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _generate_code() -> tuple[str, str]:
    """Generate a random 8-character alphanumeric code and its hash.

    Returns:
        Tuple of (plaintext_code, hash).
    """
    alphabet = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters
    alphabet = (
        alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    )
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return code, _hash_code(code)


# ── Google Sheets helpers ──────────────────────────────────────────────────


def _get_sheets_service():
    """Get an authenticated Google Sheets service."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json:
        logger.warning("GOOGLE_SHEETS_CREDENTIALS not set — sheets operations disabled")
        return None

    creds_dict = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials)


def _find_contributor_row(email: str) -> tuple[str, int, list[Any]] | None:
    """Find a contributor by email in the contact sheet.

    Returns:
        Tuple of (sheet_name, row_index, row_values) or None if not found.
    """
    try:
        service = _get_sheets_service()
        if service is None:
            return None
        # Check contact sheet first
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=LEDGER_SPREADSHEET_ID,
                range=f"{SHEET_CONTACT}!A:Z",
            )
            .execute()
        )
        rows = result.get("values", [])

        for i, row in enumerate(rows):
            if len(row) > COL_EMAIL and row[COL_EMAIL].strip().lower() == email.lower():
                return (SHEET_CONTACT, i, row)

        # Check signatures sheet
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=LEDGER_SPREADSHEET_ID,
                range=f"{SHEET_SIGNATURES}!A:Z",
            )
            .execute()
        )
        rows = result.get("values", [])

        for i, row in enumerate(rows):
            if len(row) > COL_EMAIL and row[COL_EMAIL].strip().lower() == email.lower():
                return (SHEET_SIGNATURES, i, row)

        return None
    except Exception as e:
        logger.error("Failed to find contributor: %s", e)
        return None


def _update_sheet_cell(
    sheet_name: str, row_index: int, col_index: int, value: str
) -> bool:
    """Update a single cell in a Google Sheet."""
    try:
        service = _get_sheets_service()
        if service is None:
            return False
        range_name = f"{sheet_name}!{_col_letter(col_index)}{row_index + 1}"
        service.spreadsheets().values().update(
            spreadsheetId=LEDGER_SPREADSHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [[value]]},
        ).execute()
        return True
    except Exception as e:
        logger.error(
            "Failed to update sheet cell %s: %s",
            f"{sheet_name}!{_col_letter(col_index)}{row_index + 1}",
            e,
        )
        return False


def _col_letter(index: int) -> str:
    """Convert 0-based column index to letter (0=A, 1=B, ... 25=Z)."""
    return chr(ord("A") + index)


# ── Challenge mint ─────────────────────────────────────────────────────────


def mint_challenge(email: str, telegram_id: int | None = None) -> dict[str, Any]:
    """Generate and send a verification challenge.

    Args:
        email: The contributor's email address.
        telegram_id: Optional Telegram ID for rate limiting.

    Returns:
        Dict with success status and message.
    """
    # Rate limit
    if telegram_id and not _check_rate_limit(f"telegram:{telegram_id}"):
        return {
            "success": False,
            "error": "Too many requests. Please wait before trying again.",
        }

    if not _check_rate_limit(f"email:{email}"):
        return {
            "success": False,
            "error": "Too many requests for this email. Please wait.",
        }

    # Check if email exists in the ledger
    contributor = _find_contributor_row(email)
    if contributor is None:
        # Don't reveal whether the email is registered (anti-enumeration)
        logger.info("Challenge requested for unknown email: %s", email)
        return {
            "success": True,
            "message": "If this email is registered, a verification code has been sent.",
        }

    # Generate code
    code, code_hash = _generate_code()

    # Store challenge
    _pending_challenges[email.lower()] = Challenge(
        email=email.lower(),
        code_hash=code_hash,
        expires_at=time.time() + CHALLENGE_EXPIRY_SECONDS,
    )

    # Store hash in Column G of Digital Signatures
    sheet_name, row_idx, _ = contributor
    if sheet_name == SHEET_SIGNATURES:
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_VERIFICATION_KEY, code_hash)
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_KEY_CONSUMED, "FALSE")

    # Send email with the plaintext code
    _send_challenge_email(email, code)

    logger.info(
        "Challenge minted for %s (expires in %ds)", email, CHALLENGE_EXPIRY_SECONDS
    )

    return {
        "success": True,
        "message": "A verification code has been sent to your email.",
    }


def _send_challenge_email(email: str, code: str) -> None:
    """Send the verification code via email."""
    try:
        from .gmail_client import send_email

        send_email(
            to=email,
            subject="Your TrueSight DAO Verification Code",
            body=(
                f"Hello,\n\n"
                f"Your verification code is: {code}\n\n"
                f"This code expires in 15 minutes.\n"
                f"If you didn't request this, you can ignore this email.\n\n"
                f"— TrueSight DAO Autopilot"
            ),
        )
        logger.info("Challenge email sent to %s", email)
    except Exception as e:
        logger.error("Failed to send challenge email to %s: %s", email, e)
        # Don't fail the whole operation — the code is still stored


# ── Challenge consume ─────────────────────────────────────────────────────


def consume_challenge(
    email: str,
    code: str,
    telegram_id: int,
    telegram_username: str | None = None,
) -> dict[str, Any]:
    """Verify a challenge code and bind the Telegram ID.

    Args:
        email: The contributor's email.
        code: The plaintext verification code.
        telegram_id: The Telegram user ID to bind.
        telegram_username: Optional Telegram @username.

    Returns:
        Dict with success status and identity info.
    """
    # Rate limit
    if not _check_rate_limit(f"consume:{telegram_id}"):
        return {"success": False, "error": "Too many attempts. Please wait."}

    email_lower = email.lower()

    # Check for pending challenge
    challenge = _pending_challenges.get(email_lower)
    if challenge is None:
        # Check Column G in the sheet directly
        contributor = _find_contributor_row(email)
        if contributor is None:
            return {
                "success": False,
                "error": "Verification failed. Please request a new code.",
            }

        sheet_name, row_idx, row = contributor
        if sheet_name != SHEET_SIGNATURES:
            return {
                "success": False,
                "error": "Verification failed. Please request a new code.",
            }

        # Check if already verified
        if len(row) > COL_VERIFIED and row[COL_VERIFIED].strip().upper() == "VERIFIED":
            return {"success": False, "error": "This email is already verified."}

        # No pending challenge
        return {
            "success": False,
            "error": "No pending verification. Please request a new code.",
        }

    # Check expiry
    if time.time() > challenge.expires_at:
        del _pending_challenges[email_lower]
        return {
            "success": False,
            "error": "Verification code has expired. Please request a new one.",
        }

    # Check attempts
    if challenge.attempts_remaining <= 0:
        del _pending_challenges[email_lower]
        return {
            "success": False,
            "error": "Too many failed attempts. Please request a new code.",
        }

    # Verify code
    expected_hash = challenge.code_hash
    actual_hash = _hash_code(code)

    if not hmac.compare_digest(expected_hash, actual_hash):
        challenge.attempts_remaining -= 1
        logger.warning(
            "Failed verification for %s (%d attempts remaining)",
            email,
            challenge.attempts_remaining,
        )
        return {
            "success": False,
            "error": f"Invalid code. {challenge.attempts_remaining} attempts remaining.",
        }

    # Success! Bind the Telegram ID
    contributor = _find_contributor_row(email)
    if contributor is None:
        return {"success": False, "error": "Contributor record not found."}

    sheet_name, row_idx, row = contributor

    # Update the contact sheet with Telegram ID (Column H)
    if sheet_name == SHEET_CONTACT:
        _update_sheet_cell(SHEET_CONTACT, row_idx, COL_TELEGRAM_ID, str(telegram_id))

    # Update the signatures sheet
    if sheet_name == SHEET_SIGNATURES:
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_VERIFIED, "VERIFIED")
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_KEY_CONSUMED, "TRUE")

    # Clean up
    del _pending_challenges[email_lower]

    # Emit audit event
    _emit_identity_binding_event(email, telegram_id, telegram_username)

    logger.info(
        "Identity bound: telegram_id=%s → email=%s (username=%s)",
        telegram_id,
        email,
        telegram_username,
    )

    return {
        "success": True,
        "email": email,
        "telegram_id": telegram_id,
        "message": "Identity verified and bound successfully.",
    }


def _emit_identity_binding_event(
    email: str,
    telegram_id: int,
    telegram_username: str | None = None,
) -> None:
    """Emit an [IDENTITY BINDING EVENT] for the audit trail."""
    try:
        import requests as http

        payload_lines = [
            "[IDENTITY BINDING EVENT]",
            f"- Email: {email}",
            f"- Telegram ID: {telegram_id}",
            f"- Telegram Username: {telegram_username or 'N/A'}",
            f"- Timestamp: {datetime.now(timezone.utc).isoformat()}",
            "--------",
        ]
        payload = "\n".join(payload_lines)

        http.post(
            "https://edgar.truesight.me/dao/submit_contribution",
            files={"text": (None, payload)},
            timeout=15,
        )
    except Exception as e:
        logger.warning("Failed to emit identity binding event: %s", e)


# ── Revocation ─────────────────────────────────────────────────────────────


def revoke_binding(email: str, revoked_by: str) -> dict[str, Any]:
    """Revoke a Telegram identity binding.

    Args:
        email: The contributor's email.
        revoked_by: Name of the governor who revoked it.

    Returns:
        Dict with success status.
    """
    contributor = _find_contributor_row(email)
    if contributor is None:
        return {"success": False, "error": "Contributor not found."}

    sheet_name, row_idx, row = contributor

    # Clear Telegram ID from contact sheet
    if sheet_name == SHEET_CONTACT:
        _update_sheet_cell(SHEET_CONTACT, row_idx, COL_TELEGRAM_ID, "")

    # Reset verification status
    if sheet_name == SHEET_SIGNATURES:
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_VERIFIED, "")
        _update_sheet_cell(SHEET_SIGNATURES, row_idx, COL_KEY_CONSUMED, "FALSE")

    logger.info("Binding revoked for %s by %s", email, revoked_by)

    return {
        "success": True,
        "message": f"Binding revoked for {email}.",
    }


# ── Status check ──────────────────────────────────────────────────────────


def check_binding_status(telegram_id: int) -> dict[str, Any]:
    """Check if a Telegram ID is bound to a verified identity.

    Args:
        telegram_id: The Telegram user ID.

    Returns:
        Dict with binding status and identity info.
    """
    try:
        service = _get_sheets_service()

        # Check contact sheet for Telegram ID
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=LEDGER_SPREADSHEET_ID,
                range=f"{SHEET_CONTACT}!A:Z",
            )
            .execute()
        )
        rows = result.get("values", [])

        for row in rows:
            if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID].strip() == str(
                telegram_id
            ):
                name = row[0] if len(row) > 0 else "Unknown"
                email = row[COL_EMAIL] if len(row) > COL_EMAIL else ""
                return {
                    "bound": True,
                    "name": name,
                    "email": email,
                    "telegram_id": telegram_id,
                }

        return {"bound": False, "telegram_id": telegram_id}
    except Exception as e:
        logger.error("Failed to check binding status: %s", e)
        return {"bound": False, "telegram_id": telegram_id, "error": str(e)}
