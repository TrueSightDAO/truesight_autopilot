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
from .vault import Vault, get_vault, reset_vault_for_testing

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


# ── API endpoints ──────────────────────────────────────────────────────────


@router.get("/api/credentials")
async def list_credentials(request: Request, identity: dict = Depends(_require_vault_governor)):
    """List all credential names + metadata (never values)."""
    try:
        vault = get_vault()
        refs = vault.list_refs()
        return JSONResponse({
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
        })
    except Exception as e:
        logger.error("Failed to list credentials: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/credentials")
async def add_credential(request: Request, identity: dict = Depends(_require_vault_governor)):
    """Add a new credential."""
    body = await request.json()
    name = body.get("name", "").strip()
    value = body.get("value", "").strip()
    purpose = body.get("purpose", "").strip()
    scopes_raw = body.get("scopes", "").strip()

    if not name or not value:
        raise HTTPException(status_code=400, detail="Name and value are required.")

    scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()] if scopes_raw else []

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
async def delete_credential(name: str, request: Request, identity: dict = Depends(_require_vault_governor)):
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
async def rotate_credential(name: str, request: Request, identity: dict = Depends(_require_vault_governor)):
    """Rotate (update) a credential to a new version."""
    body = await request.json()
    new_value = body.get("value", "").strip()
    new_purpose = body.get("purpose", "").strip() or None

    if not new_value:
        raise HTTPException(status_code=400, detail="New value is required.")

    try:
        vault = get_vault()
        entry = vault.update(name, new_value, identity["name"], new_purpose=new_purpose)
        return JSONResponse({
            "success": True,
            "name": name,
            "new_version": entry.version,
        })
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to rotate credential: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/audit-log")
async def get_audit_log(request: Request, identity: dict = Depends(_require_vault_governor)):
    """Get the vault audit log."""
    try:
        vault = get_vault()
        entries = vault.get_audit_log(limit=200)
        return JSONResponse({
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
        })
    except Exception as e:
        logger.error("Failed to read audit log: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/system-status")
async def system_status(request: Request, identity: dict = Depends(_require_vault_governor)):
    """Get system status including active tracks and deploy readiness."""
    from .deploy_watcher import get_system_status as _get_status
    return JSONResponse(_get_status())


@router.post("/api/deploy")
async def trigger_deploy(request: Request, identity: dict = Depends(_require_vault_governor)):
    """Trigger a deploy, optionally forcing it."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    force = body.get("force", False)
    
    from .deploy_watcher import can_deploy as _can_deploy
    ok, blocking = _can_deploy(force=force)
    
    if not ok:
        return JSONResponse({
            "success": False,
            "message": "Deploy blocked by active tracks.",
            "blocking_tracks": blocking,
        }, status_code=409)
    
    # Trigger the deploy
    import asyncio
    asyncio.create_task(_run_deploy())
    
    return JSONResponse({
        "success": True,
        "message": "Deploy triggered. Service will restart shortly.",
    })


async def _run_deploy():
    """Run the deploy in the background."""
    import subprocess
    import sys
    
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.deploy"],
            capture_output=True, text=True, timeout=300,
        )
        logger.info("Deploy result: rc=%d, stdout=%s", result.returncode, result.stdout[-500:])
    except Exception as e:
        logger.error("Deploy failed: %s", e)


@router.get("/api/health")
async def vault_health():
    """Check if the vault is initialized and healthy."""
    try:
        vault = get_vault()
        initialized = vault.is_initialized()
        count = len(vault.list_refs()) if initialized else 0
        return JSONResponse({
            "initialized": initialized,
            "credential_count": count,
            "status": "healthy" if initialized else "not_initialized",
        })
    except Exception as e:
        return JSONResponse({
            "initialized": False,
            "credential_count": 0,
            "status": f"error: {e}",
        })
