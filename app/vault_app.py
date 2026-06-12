"""Standalone vault web server — runs on port 8002, separate from the main bot.

This allows the vault page and system status to remain responsive even when
the main bot is busy with long-running LLM calls.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .vault_routes import router as vault_router

logger = logging.getLogger("autopilot.vault_app")

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

# Mount vault routes at /vault
app.include_router(vault_router)

# Also mount at root for direct access
from .vault_routes import router as vault_router_root
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
