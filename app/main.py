"""FastAPI application for truesight_autopilot."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import settings
from .email_poller import EmailPoller
from .aws_monitor import AWSMonitor

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger("autopilot")

email_poller: EmailPoller | None = None
aws_monitor: AWSMonitor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global email_poller, aws_monitor
    logger.info("Autopilot starting up...")

    if not settings.dry_run:
        email_poller = EmailPoller()
        aws_monitor = AWSMonitor()
        # Background polling loops
        asyncio.create_task(email_poller.run_loop())
        asyncio.create_task(aws_monitor.run_loop())
    else:
        logger.info("DRY_RUN=true — no background tasks started")

    yield

    logger.info("Autopilot shutting down...")


app = FastAPI(
    title="TrueSight Autopilot",
    description="Autonomous SRE + developer for TrueSight DAO",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "dry_run": settings.dry_run,
        "github_pat_set": bool(settings.github_pat),
        "gmail_token_set": bool(settings.gmail_token_json),
        "deepseek_key_set": bool(settings.deepseek_api_key),
    }


@app.post("/webhook/github")
async def github_webhook(payload: dict):
    """Receive GitHub webhook events (workflow failures, etc.)."""
    logger.info("GitHub webhook received: %s", payload.get("action", "unknown"))
    return {"status": "received"}


@app.get("/metrics")
async def metrics():
    """Prometheus-style metrics placeholder."""
    return JSONResponse(content={"prs_opened_today": 0, "emails_processed": 0})


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
