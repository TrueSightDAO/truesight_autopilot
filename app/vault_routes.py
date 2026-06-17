"""Vault web page routes — Phase 3.3, 3.5, 3.6.

Authenticates via email→RSA flow (reuses /auth/challenge), then checks
the Governors cache. Governors see the vault UI; non-governors get a
friendly contribution-nudge denial.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .auth import verify_jwt
from .governor_registry import resolve_key
from .vault import get_vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vault", tags=["vault"])

# Templates — relative to app/
_templates_dir = Path(__file__).resolve().parent / "templates" / "vault"
_templates = Jinja2Templates(directory=str(_templates_dir))


# ── Helpers ────────────────────────────────────────────────────────────────


def _resolve_identity_from_jwt(public_key_b64: str) -> dict:
    """Resolve a JWT subject (public key) to a governor identity.

    Uses resolve_key() for a fast SHA-256 content-addressed lookup
    against per-key files in treasury-cache. Falls back to the old
    load_governors() monolith only if resolve_key returns None.

    Returns dict with {name, is_governor, email}.
    """

    result = resolve_key(public_key_b64)
    if result is not None:
        return {
            "name": result.get("name", "Unknown"),
            "is_governor": "governor" in result.get("roles", []),
            "email": result.get("email", ""),
        }

    # Fallback: try the old monolith (for enumeration callers)
    from .governor_registry import load_governors as _load_govs
    data = _load_govs()
    for g in data.get("governors", []):
        if g.get("public_key") == public_key_b64:
            return {
                "name": g.get("name", "Unknown"),
                "is_governor": True,
                "email": g.get("email", ""),
            }

    # Key is verified but not in governors cache — authenticated non-governor
    return {
        "name": "Verified Contributor",
        "is_governor": False,
        "email": "",
    }


def _optional_identity(request: Request) -> dict | None:
    """Resolve identity from the JWT cookie if present, else None.

    Used by pages that render for both signed-in and anonymous visitors so the
    nav can show "Sign out" vs "Sign in" correctly (base.html keys off `identity`).
    """
    try:
        public_key = verify_jwt(request)
    except HTTPException:
        return None
    return _resolve_identity_from_jwt(public_key)


def _require_vault_governor(request: Request) -> dict:
    """Dependency: verify JWT and check governor status.

    Returns the identity dict on success. Raises 401/403 on failure.
    """
    try:
        public_key = verify_jwt(request)
    except HTTPException:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please sign in with your DAO identity.",
        )

    identity = _resolve_identity_from_jwt(public_key)
    if not identity["is_governor"]:
        raise HTTPException(
            status_code=403,
            detail="Only DAO governors may access the credential vault.",
        )
    return identity


# ── Pages ──────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def vault_page(request: Request):
    """Main vault page.

    If authenticated as governor, shows the vault UI.
    If not authenticated, shows the login prompt.
    If authenticated as non-governor, shows the contribution nudge.
    """
    error = None
    credentials = []

    # Resolve identity from the JWT cookie (None if not signed in).
    identity = _optional_identity(request)

    if identity and identity["is_governor"]:
        # Load vault and list credentials
        try:
            vault = get_vault()
            credentials = vault.list_refs()
        except Exception as e:
            logger.error("Failed to load vault: %s", e)
            error = "Could not load credential vault. It may not be initialized."

    return _templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "identity": identity,
            "credentials": credentials,
            "error": error,
            "vault_url": "/vault",
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def vault_login_page(request: Request):
    """Login page — explains the email→RSA auth flow."""
    return _templates.TemplateResponse(
        request,
        "login.html",
        {},
    )


@router.get("/logout")
async def vault_logout():
    """Sign out: clear the JWT cookie server-side and redirect to login."""
    from fastapi.responses import RedirectResponse

    response = RedirectResponse(url="/vault/login", status_code=302)
    response.set_cookie(
        key="governor_chat_session",
        value="",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=0,
        expires=0,
    )
    return response


@router.get("/status", response_class=HTMLResponse)
async def vault_status_page(request: Request):
    """System status page — shows active tracks, deploy readiness, vault health."""
    return _templates.TemplateResponse(
        request,
        "status.html",
        {"identity": _optional_identity(request)},
    )


@router.get("/followups", response_class=HTMLResponse)
async def vault_followups_page(request: Request):
    """Follow-ups monitoring page — shows all active follow-ups and their state."""
    return _templates.TemplateResponse(
        request,
        "followups.html",
        {"identity": _optional_identity(request)},
    )


# ── API endpoints ──────────────────────────────────────────────────────────


# In-memory challenge store (nonce-based, single-use)
_challenges: dict[str, dict] = {}


@router.post("/api/challenge")
async def get_challenge():
    """Generate a challenge for the client to sign with their DAO Identity keypair."""
    import secrets
    import time

    challenge = secrets.token_hex(32)
    _challenges[challenge] = {"created_at": time.time(), "used": False}
    return {"challenge": challenge}


@router.post("/api/verify-signature")
async def verify_signature(request: Request):
    """Verify a signed challenge and issue a JWT session cookie."""
    import time
    from .auth import create_jwt as _create_jwt, verify_rsa_signature

    body = await request.json()
    challenge = (body.get("challenge") or "").strip()
    signature = (body.get("signature") or "").strip()
    public_key = (body.get("public_key") or "").strip()

    if not challenge or not signature or not public_key:
        raise HTTPException(
            status_code=400, detail="challenge, signature, and public_key are required."
        )

    # Check challenge exists and hasn't been used
    stored = _challenges.get(challenge)
    if not stored:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired challenge. Please request a new one.",
        )
    if stored["used"]:
        raise HTTPException(
            status_code=400, detail="Challenge already used (replay detected)."
        )

    # Check challenge expiry (5 minutes)
    if time.time() - stored["created_at"] > 300:
        del _challenges[challenge]
        raise HTTPException(
            status_code=400, detail="Challenge expired. Please request a new one."
        )

    # Mark as used (single-use)
    stored["used"] = True

    # Verify the signature
    if not verify_rsa_signature(challenge, signature, public_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid signature. Your DAO Identity key does not match.",
        )

    # Check if this public key belongs to a governor
    identity = _resolve_identity_from_jwt(public_key)
    if not identity["is_governor"]:
        raise HTTPException(
            status_code=403,
            detail="This DAO Identity is not registered as a governor. Only DAO governors may access the credential vault.",
        )

    # Issue JWT
    token = _create_jwt(public_key)
    response = JSONResponse({"token": token, "expires_in": 3600, "identity": identity})
    response.set_cookie(
        key="governor_chat_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=3600,
    )
    return response


@router.get("/api/check-auth")
async def check_auth(request: Request):
    """Check if the current session is authenticated as a governor."""
    try:
        public_key = verify_jwt(request)
        identity = _resolve_identity_from_jwt(public_key)
        return {"authenticated": identity["is_governor"], "identity": identity}
    except HTTPException:
        return {"authenticated": False, "identity": None}


@router.post("/api/whoami")
async def whoami(request: Request):
    """Recognize a public key WITHOUT a signed-in session, so the login page can
    greet a returning governor by name *before* they sign the challenge."""
    body = await request.json()
    public_key = (body.get("public_key") or "").strip()
    if not public_key:
        return {"recognized": False}

    identity = _resolve_identity_from_jwt(public_key)
    if identity["is_governor"]:
        return {"recognized": True, "name": identity["name"], "is_governor": True}
    return {"recognized": False}


@router.get("/api/credentials")
async def list_credentials(request: Request):
    """List all credential refs (no values). Requires governor auth."""
    _require_vault_governor(request)
    vault = get_vault()
    refs = vault.list_refs()
    return {
        "credentials": [
            {
                "name": r.name,
                "purpose": r.purpose,
                "version": r.version,
                "created_by": r.created_by,
                "created_at": r.created_at,
                "scopes": r.scopes,
            }
            for r in refs
        ]
    }


@router.post("/api/credentials")
async def add_credential(request: Request):
    """Add a new credential. Requires governor auth."""
    _require_vault_governor(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    value = (body.get("value") or "").strip()
    purpose = (body.get("purpose") or "").strip()
    scopes = body.get("scopes", [])
    created_by = body.get("created_by", "Vault UI")

    if not name or not value:
        raise HTTPException(status_code=400, detail="name and value are required.")

    vault = get_vault()
    try:
        entry = vault.add(name, value, purpose, scopes, created_by)
        return {
            "success": True,
            "name": entry.name,
            "version": entry.version,
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/api/credentials/{name}")
async def delete_credential(name: str, request: Request):
    """Delete a credential. Requires governor auth."""
    _require_vault_governor(request)
    vault = get_vault()
    try:
        vault.delete(name, deleted_by="Vault UI")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/credentials/{name}/rotate")
async def rotate_credential(name: str, request: Request):
    """Rotate (update) a credential. Requires governor auth."""
    _require_vault_governor(request)
    body = await request.json()
    new_value = (body.get("value") or "").strip()
    if not new_value:
        raise HTTPException(status_code=400, detail="New value is required.")

    vault = get_vault()
    try:
        entry = vault.update(name, new_value, updated_by="Vault UI")
        return {
            "success": True,
            "name": entry.name,
            "version": entry.version,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/audit-log")
async def get_audit_log(request: Request):
    """Get the vault audit log. Requires governor auth."""
    _require_vault_governor(request)
    vault = get_vault()
    log = vault.get_audit_log()
    return {
        "entries": [
            {
                "timestamp": e.timestamp,
                "action": e.action,
                "credential_name": e.credential_name,
                "actor": e.actor,
            }
            for e in log
        ]
    }


def _git_info() -> dict:
    """Return {commit, branch} from git, or fallbacks if unavailable."""
    import subprocess
    import os as _os

    remote_dir = _os.environ.get("EC2_REMOTE_DIR", "/opt/truesight_autopilot")
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=remote_dir, text=True, timeout=5
        ).strip()
    except Exception:
        commit = _os.environ.get("AUTOPILOT_COMMIT", "unknown")
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=remote_dir, text=True, timeout=5
        ).strip()
    except Exception:
        branch = "main"
    return {"commit": commit, "branch": branch}


@router.get("/api/system-status")
async def get_system_status():
    """Get system status — active tracks, deploy readiness.

    Tracks come from the canonical deploy_watcher registry (the same one
    main.py writes chat-turn tracks to). `can_deploy()` accounts for
    heartbeat timeouts, so a stuck/expired track does not block forever.
    """
    from .deploy_watcher import _seconds_since, can_deploy, get_active_tracks

    active = get_active_tracks()
    ok, _blocking = can_deploy()
    git = _git_info()
    return {
        "can_deploy": ok,
        "total_tracks": len(active),
        "commit_hash": git["commit"],
        "active_tracks": [
            {
                "label": t.get("label"),
                "track_type": t.get("track_type"),
                "status": t.get("status"),
                "elapsed_s": _seconds_since(t.get("started_at", "")),
                "max_duration_s": t.get("expected_max_duration_s"),
            }
            for t in active
        ],
    }


@router.get("/api/runtime-config")
async def get_runtime_config():
    """Get runtime configuration (non-sensitive).

    Schema matches status.html JS — every field the frontend renders MUST
    be present. Keep this in sync with the template.
    """
    import os as _os

    from .config import settings

    git = _git_info()
    return {
        "service": "TrueSight DAO Autopilot",
        "version": _os.environ.get("AUTOPILOT_VERSION", "unknown"),
        "code_repo": "https://github.com/TrueSightDAO/truesight_autopilot",
        "git": {
            "commit": git["commit"],
            "branch": git["branch"],
        },
        "llm": {
            "provider": settings.llm_provider,
            "model": _os.environ.get("LITELLM_MODEL", settings.deepseek_model),
            "fallback_model": _os.environ.get("LLM_FALLBACK_MODEL", ""),
        },
        "context_repo": settings.agentic_context_repo,
        "transcript_repo": "https://github.com/TrueSightDAO/truesight_autopilot_transcript",
        "edgar_url": "https://edgar.truesight.me",
        "ledger_url": "https://docs.google.com/spreadsheets/d/1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU",
        "ledger_name": "TrueSight DAO Ledger",
        "vault_url": "https://sophia.truesight.me/vault",
        "python_version": __import__("sys").version,
    }


@router.post("/api/deploy")
async def trigger_deploy(request: Request):
    """Trigger a deploy. Requires governor auth.

    Normal deploy: only when no active tracks.
    Force deploy: bypasses the active-track check (use with caution).
    """
    _require_vault_governor(request)
    body = await request.json()
    force = body.get("force", False)

    from .deploy_watcher import can_deploy

    ok, blocking = can_deploy(force=force)
    if not ok:
        return {
            "success": False,
            "message": f"Cannot deploy: {len(blocking)} active track(s) running. Use force deploy to override.",
        }

    # Signal the deploy watcher
    import os
    import signal

    os.kill(os.getpid(), signal.SIGHUP)
    return {
        "success": True,
        "message": "Deploy triggered. Service will restart shortly.",
    }


@router.get("/api/health")
async def vault_health():
    """Vault health check — returns initialization status and credential count."""
    vault = get_vault()
    return {
        "initialized": vault.is_initialized(),
        "credential_count": len(vault.list_refs()) if vault.is_initialized() else 0,
    }


@router.get("/api/followups")
async def api_followups():
    """JSON endpoint returning all parsed follow-ups with their scheduling state."""
    from .followups import get_state, parse_all

    followups = parse_all()
    state = {}
    for f in followups:
        sid = f.get("id")
        if sid:
            s = get_state(sid)
            if s:
                state[sid] = s

    return {
        "followups": followups,
        "state": state,
        "open_count": len([f for f in followups if f.get("status") == "open"]),
    }
