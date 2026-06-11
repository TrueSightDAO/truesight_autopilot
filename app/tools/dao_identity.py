"""Register a new DAO identity via Edgar (Contributors Digital Signatures).

Uses the dao_client library (already in venv) to generate an RSA keypair,
sign an [EMAIL REGISTERED EVENT], submit to Edgar, and save the new keys
to the autopilot's .env file.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.identity")


def register_identity(email: str, env_path: str | None = None) -> dict[str, Any]:
    """Generate RSA keypair, register via Edgar, and persist to .env.

    Returns a dict with:
        success: bool
        email: str
        public_key_b64: str (new key — truncated in response for safety)
        edgar_status: int
        edgar_response: dict
        error: str (if unsuccessful)
    """
    try:
        import requests as http
        from dotenv import set_key as dotenv_set_key
        from truesight_dao_client.edgar_client import (
            generate_keypair,
            load_private_key,
            load_public_key,
        )
    except ImportError as e:
        return {"success": False, "error": f"Missing dependency: {e}"}

    try:
        # 1. Generate keypair
        pub_der_b64, priv_der_b64 = generate_keypair()
        load_public_key(pub_der_b64)
        priv_key = load_private_key(priv_der_b64)

        # 2. Build and sign [EMAIL REGISTERED EVENT]
        payload_lines = [
            "[EMAIL REGISTERED EVENT]",
            f"- Email: {email}",
            "--------",
        ]
        payload = "\n".join(payload_lines)

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        sig_bytes = priv_key.sign(
            payload.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig_bytes).decode()

        # 3. Build share_text
        hashlib.sha256(sig_bytes).hexdigest()[:16]
        generation_source = "https://github.com/TrueSightDAO/truesight_autopilot"
        verify_url = "https://edgar.truesight.me/dao/submit_contribution"

        share_text = (
            f"{payload}\n"
            f"My Digital Signature: {pub_der_b64}\n"
            f"Request Transaction ID: {sig_b64}\n"
            f"This submission was generated using {generation_source}\n"
            f"Verification URL: {verify_url}"
        )

        # 4. Submit to Edgar
        resp = http.post(
            "https://edgar.truesight.me/dao/submit_contribution",
            files={"text": (None, share_text)},
            timeout=30,
        )

        edgar_data = {}
        try:
            edgar_data = resp.json()
        except Exception:
            edgar_data = {"raw": resp.text[:500]}

        if not resp.ok:
            return {
                "success": False,
                "email": email,
                "error": f"Edgar returned {resp.status_code}: {resp.text[:300]}",
                "edgar_status": resp.status_code,
                "edgar_response": edgar_data,
            }

        # 5. Save keys to .env
        dotenv_path = env_path or str(Path(__file__).resolve().parent.parent.parent / ".env")
        dotenv_set_key(dotenv_path, "EMAIL", email)
        dotenv_set_key(dotenv_path, "PUBLIC_KEY", pub_der_b64)
        dotenv_set_key(dotenv_path, "PRIVATE_KEY", priv_der_b64)

        logger.info("Registered identity %s — keys saved to %s", email, dotenv_path)

        return {
            "success": True,
            "email": email,
            "public_key_b64": pub_der_b64[:40] + "...",
            "edgar_status": resp.status_code,
            "edgar_response": edgar_data,
            "dotenv_path": dotenv_path,
        }

    except Exception as e:
        logger.error("register_identity failed: %s", e)
        return {"success": False, "email": email, "error": str(e)}


# ── capability manifest entry ─────────────────────────────────────────────

import json as _json  # noqa: E402

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="register_identity",
    description="Register a new DAO identity by generating an RSA-2048 keypair and submitting to Edgar.",
    parameters={
        "type": "object",
        "properties": {"email": {"type": "string", "description": "The email address to register."}},
        "required": ["email"],
    },
    handler=lambda args, ctx: _json.dumps(register_identity(args.get("email", "")), indent=2),
)
