"""Standalone vault web server — runs on port 8002, separate from the main bot.

This allows the vault page and system status to remain responsive even when
the main bot is busy with long-running LLM calls.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .vault_routes import router as vault_router
from .auth_routes import router as auth_router

logger = logging.getLogger("autopilot.vault_app")


class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    """Add Cache-Control: no-store to all HTML responses so the browser never
    serves a stale vault page after a deploy. Prevents the "old JS + new API
    schema" mismatch that produces "Failed to load..." errors post-deploy."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


app = FastAPI(
    title="TrueSight DAO Vault",
    description="Credential vault and system status — separate from the main bot.",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# No-cache for HTML — prevents stale-vault-page-after-deploy bugs
app.add_middleware(NoCacheHTMLMiddleware)

# Mount vault routes at /vault
app.include_router(vault_router)

# Mount auth routes
app.include_router(auth_router)

# Also mount at root for direct access
from fastapi import APIRouter

root_router = APIRouter()


@root_router.get("/")
async def root():
    return {
        "service": "TrueSight DAO Vault",
        "version": "1.0.0",
        "endpoints": {
            "vault": "/vault/",
            "login": "/vault/login",
            "health": "/vault/api/health",
            "system_status": "/vault/api/system-status",
            "credentials": "/vault/api/credentials",
            "audit_log": "/vault/api/audit-log",
        },
    }


app.include_router(root_router)


@app.on_event("startup")
async def _init_vault_on_startup() -> None:
    """Eagerly initialize the credential vault (dir + key) when the worker boots,
    so a fresh machine is ready immediately and never hits a missing-key error on
    the first credential upload. get_vault() auto-initializes; this just triggers it
    at boot instead of lazily on first request. Best-effort — never blocks startup."""
    try:
        from .vault import get_vault

        v = get_vault()
        logger.info("Vault ready on startup (initialized=%s)", v.is_initialized())
    except Exception as e:  # pragma: no cover
        logger.warning("Vault startup init failed: %s", e)
