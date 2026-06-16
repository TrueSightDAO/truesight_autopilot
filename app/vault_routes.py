"""Vault web page routes — Phase 3.3, 3.5, 3.6.

Authenticates via email→RSA flow (reuses /auth/challenge), then checks
the Governors cache. Governors see the vault UI; non-governors get a
friendly contribution-nudge denial.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .auth import verify_jwt
from .governor_registry import load_governors
from .vault import get_vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vault", tags=["vault"])

# Templates — relative to app/
_templates_dir = Path(__file__).resolve().parent / "templates" / "vault"
_templates = Jinja2Templates(directory=str(_templates_dir))


# ── Helpers ────────────────────────────────────────────────────────────────


def _resolve_identity_from_jwt(public_key_b64: str) -> dict:
    """Resolve a JWT subject (public key) to a governor identity.

    Returns dict with {name, is_governor, email} or raises 403.
    """

    data = load_governors()

    # First try direct public key match
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
    identity = None
    error = None
    credentials = []

    # Try to authenticate from the JWT cookie
    try:
        public_key = verify_jwt(request)
        identity = _resolve_identity_from_jwt(public_key)
    except HTTPException:
        pass  # Not authenticated — show login page

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
        {},
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
    greet a returning governor by name *before* they sign in. No signature is
    required (it's just an identity lookup of the caller's own key); the email is
    never returned, so it can't be used to harvest contact info."""
    body = await request.json()
    public_key = (body.get("public_key") or "").strip()
    if not public_key:
        return {"recognized": False, "is_governor": False}
    identity = _resolve_identity_from_jwt(public_key)
    if identity["is_governor"]:
        return {"recognized": True, "name": identity["name"], "is_governor": True}
    return {"recognized": False, "is_governor": False}


@router.get("/api/credentials")
async def list_credentials(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """List all credential names + metadata (never values)."""
    try:
        vault = get_vault()
        refs = vault.list_refs()
        return JSONResponse(
            {
                "credentials": [
                    {
                        "name": r.name,
                        "purpose": r.purpose,
                        "scopes": r.scopes,
                        "version": r.version,
                        "created_by": r.created_by,
                        "created_at": r.created_at,
                    }
                    for r in refs
                ]
            }
        )
    except Exception as e:
        logger.error("Failed to list credentials: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/credentials")
async def add_credential(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Add a new credential."""
    body = await request.json()
    name = body.get("name", "").strip()
    value = body.get("value", "").strip()
    purpose = body.get("purpose", "").strip()
    scopes_raw = body.get("scopes", "").strip()

    if not name or not value:
        raise HTTPException(status_code=400, detail="Name and value are required.")

    scopes = (
        [s.strip() for s in scopes_raw.split(",") if s.strip()] if scopes_raw else []
    )

    try:
        vault = get_vault()
        vault.add(name, value, purpose or name, scopes, identity["name"])
        return JSONResponse({"success": True, "name": name})
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error("Failed to add credential: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/credentials/{name}")
async def delete_credential(
    name: str, request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Delete a credential."""
    try:
        vault = get_vault()
        vault.delete(name, identity["name"])
        return JSONResponse({"success": True})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to delete credential: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/credentials/{name}/rotate")
async def rotate_credential(
    name: str, request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Rotate (update) a credential to a new version."""
    body = await request.json()
    new_value = body.get("value", "").strip()
    new_purpose = body.get("purpose", "").strip() or None

    if not new_value:
        raise HTTPException(status_code=400, detail="New value is required.")

    try:
        vault = get_vault()
        entry = vault.update(name, new_value, identity["name"], new_purpose=new_purpose)
        return JSONResponse(
            {
                "success": True,
                "name": name,
                "new_version": entry.version,
            }
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to rotate credential: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/audit-log")
async def get_audit_log(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Get the vault audit log."""
    try:
        vault = get_vault()
        entries = vault.get_audit_log(limit=200)
        return JSONResponse(
            {
                "entries": [
                    {
                        "action": e.action,
                        "credential_name": e.credential_name,
                        "version": e.version,
                        "actor": e.actor,
                        "timestamp": e.timestamp,
                        "details": e.details,
                    }
                    for e in entries
                ]
            }
        )
    except Exception as e:
        logger.error("Failed to read audit log: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _enrich_track(track: dict) -> dict:
    """Add thread_id, a clickable Telegram deep-link, and the topic name to a track,
    derived from its session_id (tg:<chat>:<thread>)."""
    sid = (track.get("metadata") or {}).get("session_id") or track.get("id") or ""
    parts = str(sid).split(":")
    if len(parts) >= 3 and parts[0] == "tg":
        chat_id, thread_id = parts[1], parts[2]
        track["thread_id"] = thread_id
        track["chat_id"] = chat_id
        # Telegram deep-link to the forum topic (strip the -100 supergroup prefix).
        if chat_id.startswith("-100"):
            track["telegram_link"] = f"https://t.me/c/{chat_id[4:]}/{thread_id}"
        try:
            from .topic_names import get_topic_name

            track["thread_name"] = get_topic_name(thread_id)
        except Exception:
            track["thread_name"] = None
    return track


@router.get("/api/system-status")
async def system_status(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Get system status including active tracks and deploy readiness."""
    from .deploy_watcher import get_system_status as _get_status

    status = _get_status()
    status["active_tracks"] = [
        _enrich_track(t) for t in status.get("active_tracks", [])
    ]
    return JSONResponse(status)



@router.get("/api/runtime-config")
async def runtime_config(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Get runtime configuration — commit hash, context repo, transcript repo, LLM provider, etc.
    
    Useful for debugging and for operators setting up their own instance.
    """
    import subprocess
    import os
    from pathlib import Path

    def _get_git_info():
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=Path(__file__).resolve().parent.parent,
            )
            commit = r.stdout.strip() if r.returncode == 0 else "unknown"
        except Exception:
            commit = "unknown"

        try:
            r = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
                cwd=Path(__file__).resolve().parent.parent,
            )
            origin = r.stdout.strip() if r.returncode == 0 else "unknown"
        except Exception:
            origin = "unknown"

        try:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=Path(__file__).resolve().parent.parent,
            )
            branch = r.stdout.strip() if r.returncode == 0 else "unknown"
        except Exception:
            branch = "unknown"

        return {"commit": commit, "origin": origin, "branch": branch}

    git_info = _get_git_info()

    # Context repos
    context_repo = os.getenv("AGENTIC_CONTEXT_REPO", "https://github.com/TrueSightDAO/agentic_ai_context.git")
    transcript_repo = "https://github.com/TrueSightDAO/truesight_autopilot_transcript"

    # LLM config
    llm_provider = os.getenv("LLM_PROVIDER", "deepseek")
    litellm_model = os.getenv("LITELLM_MODEL", "")
    deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    bigmodel_model = os.getenv("BIGMODEL_MODEL", "glm-4.5")

    # Non-secret env vars
    safe_env = {}
    secret_suffixes = ("KEY", "SECRET", "TOKEN", "PAT", "PASSWORD", "HASH", "API_", "TOKENS_DIR")
    for k, v in sorted(os.environ.items()):
        if any(k.upper().endswith(s) or k.upper().startswith(s) for s in secret_suffixes):
            continue
        if k.startswith("_"):
            continue
        safe_env[k] = v

    return {
        "service": "TrueSight DAO Autopilot",
        "version": "1.0.0",
        "git": git_info,
        "code_repo": "https://github.com/TrueSightDAO/truesight_autopilot",
        "context_repo": context_repo,
        "transcript_repo": transcript_repo,
        "llm": {
            "provider": llm_provider,
            "model": litellm_model or deepseek_model,
            "fallback_model": bigmodel_model,
        },
        "environment": safe_env,
    }


@router.post("/api/deploy")
async def trigger_deploy(
    request: Request, identity: dict = Depends(_require_vault_governor)
):
    """Trigger a deploy, optionally forcing it."""
    body = (
        await request.json()
        if request.headers.get("content-type") == "application/json"
        else {}
    )
    force = body.get("force", False)

    from .deploy_watcher import can_deploy as _can_deploy

    ok, blocking = _can_deploy(force=force)

    if not ok:
        return JSONResponse(
            {
                "success": False,
                "message": "Deploy blocked by active tracks.",
                "blocking_tracks": blocking,
            },
            status_code=409,
        )

    # Trigger the deploy
    import asyncio

    asyncio.create_task(_run_deploy())

    return JSONResponse(
        {
            "success": True,
            "message": "Deploy triggered. Service will restart shortly.",
        }
    )


async def _run_deploy():
    """Run the deploy in the background."""
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.deploy"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        logger.info(
            "Deploy result: rc=%d, stdout=%s", result.returncode, result.stdout[-500:]
        )
    except Exception as e:
        logger.error("Deploy failed: %s", e)


@router.get("/api/health")
async def vault_health():
    """Check if the vault is initialized and healthy."""
    try:
        vault = get_vault()
        initialized = vault.is_initialized()
        count = len(vault.list_refs()) if initialized else 0
        return JSONResponse(
            {
                "initialized": initialized,
                "credential_count": count,
                "status": "healthy" if initialized else "not_initialized",
            }
        )
    except Exception as e:
        return JSONResponse(
            {
                "initialized": False,
                "credential_count": 0,
                "status": f"error: {e}",
            }
        )
