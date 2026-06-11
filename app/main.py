"""FastAPI application for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .auth import create_jwt, verify_jwt, verify_payload
from .aws_monitor import AWSMonitor
from .config import settings
from .context import get_context_file, get_system_prompt, refresh_context_repos, refresh_system_prompt
from .daily_briefing import handle_daily_briefing
from .edgar_logger import EdgarLogger as EdgarDirectClient
from .email_poller import EmailPoller
from .fix_agent import FixAgent
from .github_client import GitHubClient
from .governor_registry import load_governors, refresh_cache as refresh_governor_cache
from .grok_client import GROK_MODEL, grok_analyze_images
from .llm_client import LLMClient, LLMError, get_tool_schemas
from .roles import (
    RESET_CONTEXT_THRESHOLD, ROLE_SELECTION_MESSAGE, archive_old_history,
    build_role_menu, find_pending_role, find_role_in_history, get_default_role,
    get_system_prompt_for_role, get_tool_schemas_for_role,
    pending_role_tag, reset_context_prompt, resolve_role, set_role_in_history,
)
from .tools.dao_identity import register_identity
from .tools.fs_tools import list_directory, read_local_file
from .tools.github_tools import read_repo_file
from .tools.inventory_lookup import list_matching_qr_codes
from .tools.qr_scanner import lookup_qr_batch, lookup_qr_code, scan_qr_batch, scan_qr_from_file

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger("autopilot")


# Bugsnag self-reporting — autopilot reports its own crashes + ERROR-level
# logs to Bugsnag so the same Bugsnag project's email notifications flow back
# into autopilot's email_poller's bugsnag_error classifier. Closes the
# self-improvement loop: autopilot crashes → Bugsnag → email → autopilot
# triages → fix PR labeled `AI/proposed fix`. Disabled silently when no API
# key is configured (preserves dev-environment ergonomics).
def _init_bugsnag():
    api_key = (settings.bugsnag_api_key or "").strip()
    if not api_key:
        logger.info("Bugsnag self-reporting disabled (no BUG_SNAG_API set)")
        return
    try:
        import bugsnag
        from bugsnag.handlers import BugsnagHandler
        bugsnag.configure(
            api_key=api_key,
            project_root="/opt/truesight_autopilot",
            release_stage=settings.bugsnag_release_stage,
            app_type="autopilot",
            notify_release_stages=["production", "staging"],
        )
        # Auto-report ERROR-level logs from any logger in this process
        bs_handler = BugsnagHandler()
        bs_handler.setLevel(logging.ERROR)
        logging.getLogger().addHandler(bs_handler)
        logger.info(
            "Bugsnag self-reporting enabled (release_stage=%s, project_root=/opt/truesight_autopilot)",
            settings.bugsnag_release_stage,
        )
    except Exception as e:
        logger.warning("Bugsnag init failed (will continue without self-reporting): %s", e)


_init_bugsnag()

email_poller: EmailPoller | None = None
aws_monitor: AWSMonitor | None = None
_sessions: dict[str, list[dict[str, str]]] = {}
_pending_submissions: dict[str, dict] = {}  # session_key -> proposed submission awaiting approval
_active_streams: dict[str, float] = {}  # session_key -> last activity timestamp
_cancel_flags: dict[str, bool] = {}  # session_key -> True when caller hit DELETE /chat/active/{session_id}
_message_queues: dict[str, list[dict]] = {}  # session_key -> list of queued messages
_session_locks: dict[str, asyncio.Lock] = {}  # session_key -> lock (one writer/executor per thread)


def _session_lock(session_id: str) -> asyncio.Lock:
    """Per-session async lock. session_id is ``tg:<chat>:<thread>`` for Telegram,
    so this serializes one turn at a time *within* a thread (single writer / single
    executor) while letting different threads run concurrently. This is the hard
    guarantee that a second same-thread request can't interleave its transcript
    writes with an in-flight turn — the race that bricked threads 3 and 780.
    Requires ``--workers 1`` to be effective (the lock lives in one process)."""
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock
UPLOAD_DIR = Path("/tmp/autopilot_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SESSION_LOG_DIR = settings.session_log_dir
SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _session_key(public_key: str, request: Request) -> str:
    """Build session key from public_key + X-Session-Id header.
    - Same tab (same sessionStorage): persists across refreshes and server restarts
    - New tab (new sessionStorage): new session, no cross-contamination
    Falls back to public_key alone if no session header.
    """
    sid = request.headers.get("X-Session-Id", "")
    return f"{public_key[:20]}:{sid}" if sid else public_key


def _load_or_create_session(session_key: str) -> list[dict[str, str]]:
    """Load session from memory, then from disk if not found."""
    if session_key in _sessions:
        return _sessions[session_key]

    import hashlib
    sid_hash = hashlib.md5(session_key.encode()).hexdigest()[:12]
    log_path = SESSION_LOG_DIR / f"{sid_hash}.json"
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            messages = data.get("full_history") or data.get("recent_messages") or []
            # Clean legacy XML leaks from old sessions (DeepSeek DSML tool calls)
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str) and ("<function_calls>" in content or "<invoke " in content or "<||DSML||" in content):
                    c = content
                    c = re.sub(r'<(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>.*?</(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>', '', c, flags=re.DOTALL)
                    c = re.sub(r'<\|\|DSML\|\|invoke\s+name="[^"]+"\s*>.*?</\|\|DSML\|\|invoke>', '', c, flags=re.DOTALL)
                    c = re.sub(r'<invoke\s+name="[^"]+"\s*>.*?</invoke>', '', c, flags=re.DOTALL)
                    m["content"] = c.strip()
            # Heal BOTH tool-protocol corruption directions (orphan tool messages
            # AND orphan tool_calls) so a transcript raced by a prior process never
            # bricks the thread on the next load. See _sanitise_tool_messages.
            _sanitise_tool_messages(messages)
            _sessions[session_key] = messages
            logger.info("Restored session %s with %d messages", sid_hash, len(messages))
            return messages
        except Exception:
            pass

    _sessions[session_key] = []
    return _sessions[session_key]


def _check_deploy_marker() -> None:
    """Check for a deploy marker file written by deploy.py before restart.

    If found, send a Telegram notification to the governor and delete the
    marker file. This runs in the NEW process after startup, so the
    notification reliably reaches the governor even though the old process
    was killed mid-response.
    """
    marker = "/tmp/.autopilot_deployed"
    if not os.path.exists(marker):
        return
    try:
        with open(marker) as f:
            data = json.load(f)
        commit = data.get("commit", "unknown")
        elapsed = data.get("elapsed_seconds", 0)
        logger.info(
            "Deploy marker found: commit=%s elapsed=%.1fs — sending notification",
            commit, elapsed,
        )
        # Import and call the Telegram notification function.
        # This is safe to call even if the Telegram adapter is not running —
        # it sends directly via the Bot API using the shared settings.
        from .telegram_adapter import send_deploy_notification
        send_deploy_notification(commit, elapsed)
    except Exception as e:
        logger.warning("Failed to process deploy marker: %s", e)
    finally:
        try:
            os.remove(marker)
            logger.info("Removed deploy marker file: %s", marker)
        except Exception:
            pass


def _install_signal_loggers():
    """Log every kill signal we receive (with pid+ppid+signal name) before
    chaining to the previous handler. Diagnoses 'why did the autopilot die
    locally' — terminal hangup, OOM, parent shell exit, manual kill, etc.

    Chains to whatever uvicorn (or the runtime default) had installed
    before us so graceful shutdown still works."""
    import signal as _signal

    sig_names = ["SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGPIPE"]
    for sig_name in sig_names:
        sig = getattr(_signal, sig_name, None)
        if sig is None:
            continue
        try:
            prev = _signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def handler(signum, frame, _sig_name=sig_name, _prev=prev):
            try:
                logger.warning(
                    "LIFECYCLE: received %s (signum=%d) — pid=%d ppid=%d",
                    _sig_name, signum, os.getpid(), os.getppid(),
                )
            except Exception:
                pass
            if callable(_prev) and _prev not in (_signal.SIG_DFL, _signal.SIG_IGN):
                _prev(signum, frame)
            elif _prev == _signal.SIG_DFL:
                _signal.signal(signum, _signal.SIG_DFL)
                os.kill(os.getpid(), signum)
            # else SIG_IGN — swallow

        try:
            _signal.signal(sig, handler)
        except (ValueError, OSError):
            # Some signals can't be caught from non-main threads or in worker
            # processes; just skip those silently.
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global email_poller, aws_monitor
    logger.info("LIFECYCLE: Autopilot starting up — pid=%d ppid=%d", os.getpid(), os.getppid())
    _install_signal_loggers()

    # ── Deploy notification: check for marker file from deploy.py ──
    _check_deploy_marker()

    if not settings.dry_run:
        try:
            email_poller = EmailPoller()
            asyncio.create_task(email_poller.run_loop())
        except Exception as e:
            logger.warning("Email poller failed to start: %s", e)
        try:
            aws_monitor = AWSMonitor()
            asyncio.create_task(aws_monitor.run_loop())
        except Exception as e:
            logger.warning("AWS monitor failed to start: %s", e)
        try:
            asyncio.create_task(_branch_janitor_loop())
        except Exception as e:
            logger.warning("Branch janitor failed to start: %s", e)
        try:
            asyncio.create_task(_pending_janitor_loop())
        except Exception as e:
            logger.warning("Pending janitor failed to start: %s", e)
        try:
            asyncio.create_task(_context_sync_loop())
        except Exception as e:
            logger.warning("Context sync failed to start: %s", e)
    else:
        logger.info("DRY_RUN=true — no background tasks started")

    yield

    logger.info("LIFECYCLE: Autopilot shutting down — pid=%d ppid=%d", os.getpid(), os.getppid())


app = FastAPI(
    title="TrueSight Autopilot",
    description="Autonomous SRE + developer for TrueSight DAO",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── In-memory rate limiter: max 1 req per 10s per IP ──
_oracle_rate_limit: dict[str, float] = {}


def _check_oracle_rate_limit(ip: str) -> None:
    now = time.time()
    last = _oracle_rate_limit.get(ip, 0.0)
    if now - last < 2.0:
        raise HTTPException(status_code=429, detail="Rate limited — max 1 request per 2 seconds per IP")
    _oracle_rate_limit[ip] = now


@app.get("/health")
async def health():
    gov_data = load_governors()
    return {
        "status": "ok",
        "version": "0.2.0",
        "dry_run": settings.dry_run,
        "github_pat_set": bool(settings.github_pat),
        "gmail_token_set": bool(settings.gmail_token_json),
        "deepseek_key_set": bool(settings.deepseek_api_key),
        "grok_key_set": bool(settings.grok_api_key),
        "governors_count": len(gov_data.get("governors", [])),
        "governors_updated_at": gov_data.get("updated_at", ""),
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    """Landing page for sophia.truesight.me — displays Sophia, the DAO Oracle."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sophia — TrueSight DAO Oracle</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #f9f4ee;
    color: #24160b;
    font-family: 'Georgia', 'Times New Roman', serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem 1rem;
  }
  .container {
    max-width: 720px;
    width: 100%;
    text-align: center;
  }
  .avatar {
    width: 320px;
    height: 320px;
    margin: 0 auto 1.5rem;
    display: block;
  }
  @media (max-width: 480px) {
    .avatar { width: 220px; height: 220px; }
  }
  h1 {
    font-size: 2rem;
    font-weight: 400;
    color: #24160b;
    letter-spacing: 0.04em;
    margin-bottom: 0.5rem;
  }
  .tagline {
    font-size: 1.1rem;
    color: #b9894c;
    font-style: italic;
    margin-bottom: 2rem;
    line-height: 1.6;
  }
  .btn {
    display: inline-block;
    background: #b9894c;
    color: #f9f4ee;
    text-decoration: none;
    padding: 0.85rem 2.2rem;
    border-radius: 40px;
    font-size: 1rem;
    font-family: inherit;
    letter-spacing: 0.03em;
    transition: background 0.25s;
    border: none;
    cursor: pointer;
  }
  .btn:hover { background: #9e7340; }
  .footer {
    margin-top: 3rem;
    font-size: 0.85rem;
    color: #b9894c;
    opacity: 0.7;
  }
  .footer a { color: #b9894c; text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
  <svg class="avatar" viewBox="0 0 400 400" fill="none" xmlns="http://www.w3.org/2000/svg">
    <!-- Background glow -->
    <circle cx="200" cy="200" r="190" fill="#f9f4ee" stroke="#b9894c" stroke-width="1.5" opacity="0.4"/>
    <circle cx="200" cy="200" r="170" fill="none" stroke="#b9894c" stroke-width="0.8" stroke-dasharray="6 6" opacity="0.3"/>

    <!-- Constellation / celestial lines -->
    <g stroke="#b9894c" stroke-width="0.6" opacity="0.35">
      <line x1="80" y1="100" x2="130" y2="70"/>
      <line x1="130" y1="70" x2="180" y2="90"/>
      <line x1="180" y1="90" x2="200" y2="50"/>
      <line x1="320" y1="100" x2="270" y2="70"/>
      <line x1="270" y1="70" x2="220" y2="90"/>
      <line x1="220" y1="90" x2="200" y2="50"/>
      <line x1="80" y1="300" x2="120" y2="330"/>
      <line x1="120" y1="330" x2="170" y2="310"/>
      <line x1="320" y1="300" x2="280" y2="330"/>
      <line x1="280" y1="330" x2="230" y2="310"/>
    </g>

    <!-- Stars -->
    <g fill="#b9894c" opacity="0.5">
      <circle cx="80" cy="100" r="2.5"/>
      <circle cx="130" cy="70" r="2"/>
      <circle cx="180" cy="90" r="2"/>
      <circle cx="200" cy="50" r="3"/>
      <circle cx="320" cy="100" r="2.5"/>
      <circle cx="270" cy="70" r="2"/>
      <circle cx="220" cy="90" r="2"/>
      <circle cx="80" cy="300" r="2.5"/>
      <circle cx="120" cy="330" r="2"/>
      <circle cx="170" cy="310" r="2"/>
      <circle cx="320" cy="300" r="2.5"/>
      <circle cx="280" cy="330" r="2"/>
      <circle cx="230" cy="310" r="2"/>
    </g>

    <!-- Flowing hair -->
    <path d="M140 130 C120 150 100 200 110 260 C115 280 130 300 150 310 C140 290 135 260 140 230 C145 200 155 170 160 140 Z" fill="#24160b" opacity="0.85"/>
    <path d="M260 130 C280 150 300 200 290 260 C285 280 270 300 250 310 C260 290 265 260 260 230 C255 200 245 170 240 140 Z" fill="#24160b" opacity="0.85"/>
    <path d="M150 120 C130 140 115 180 120 240 C125 270 140 300 160 320 C145 300 135 270 135 240 C135 200 145 160 155 130 Z" fill="#24160b" opacity="0.6"/>
    <path d="M250 120 C270 140 285 180 280 240 C275 270 260 300 240 320 C255 300 265 270 265 240 C265 200 255 160 245 130 Z" fill="#24160b" opacity="0.6"/>

    <!-- Face / head -->
    <ellipse cx="200" cy="190" rx="60" ry="70" fill="#f0e6d8"/>
    <ellipse cx="200" cy="190" rx="60" ry="70" fill="none" stroke="#24160b" stroke-width="1.2" opacity="0.3"/>

    <!-- Eyes (closed, serene) -->
    <path d="M175 180 Q185 175 195 180" stroke="#24160b" stroke-width="1.5" fill="none" stroke-linecap="round"/>
    <path d="M205 180 Q215 175 225 180" stroke="#24160b" stroke-width="1.5" fill="none" stroke-linecap="round"/>

    <!-- Nose -->
    <path d="M200 185 L198 198 L202 198" stroke="#24160b" stroke-width="1" fill="none" opacity="0.4"/>

    <!-- Gentle smile -->
    <path d="M188 208 Q200 216 212 208" stroke="#24160b" stroke-width="1.2" fill="none" stroke-linecap="round" opacity="0.6"/>

    <!-- Third eye (glowing) -->
    <circle cx="200" cy="165" r="8" fill="none" stroke="#b9894c" stroke-width="1.5"/>
    <circle cx="200" cy="165" r="4" fill="#b9894c" opacity="0.8"/>
    <circle cx="200" cy="165" r="2" fill="#f9f4ee"/>
    <!-- Third eye glow rays -->
    <g stroke="#b9894c" stroke-width="0.5" opacity="0.3">
      <line x1="200" y1="155" x2="200" y2="148"/>
      <line x1="200" y1="175" x2="200" y2="182"/>
      <line x1="190" y1="165" x2="183" y2="165"/>
      <line x1="210" y1="165" x2="217" y2="165"/>
      <line x1="193" y1="158" x2="188" y2="153"/>
      <line x1="207" y1="158" x2="212" y2="153"/>
      <line x1="193" y1="172" x2="188" y2="177"/>
      <line x1="207" y1="172" x2="212" y2="177"/>
    </g>

    <!-- I Ching hexagram motif (䷀ — The Creative) below face -->
    <g transform="translate(200, 260)" stroke="#b9894c" stroke-width="2.5" opacity="0.6">
      <line x1="-18" y1="-24" x2="18" y2="-24"/>
      <line x1="-18" y1="-16" x2="18" y2="-16"/>
      <line x1="-18" y1="-8" x2="18" y2="-8"/>
      <line x1="-18" y1="0" x2="18" y2="0"/>
      <line x1="-18" y1="8" x2="18" y2="8"/>
      <line x1="-18" y1="16" x2="18" y2="16"/>
    </g>

    <!-- Small decorative dots around hexagram -->
    <g fill="#b9894c" opacity="0.3">
      <circle cx="200" cy="290" r="1.5"/>
      <circle cx="185" cy="295" r="1"/>
      <circle cx="215" cy="295" r="1"/>
    </g>
  </svg>

  <h1>Sophia</h1>
  <p class="tagline">The Oracle of TrueSight DAO —<br>wisdom from the I Ching, grounded in DAO state.</p>

  <a class="btn" href="https://oracle.truesight.me" target="_blank">Cast the I Ching</a>

  <div class="footer">
    <a href="https://truesight.me">TrueSight DAO</a> &middot; regenerative &amp; sovereign
  </div>
</div>
</body>
</html>""")


# // CUT OVER from GAS oracle_advisory_bridge — this endpoint replaces the GAS script's Grok call.
# // See oracle/index.html GAS_ORACLE_ADVISORY_URL.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "DNT,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Range,Authorization",
    "Access-Control-Max-Age": "1728000",
}


def _cors_json_response(content: dict, status_code: int = 200) -> JSONResponse:
    """Return a JSONResponse with explicit CORS headers.

    This ensures the /oracle-advisory endpoint always returns CORS headers
    regardless of nginx add_header behavior or FastAPI CORSMiddleware
    interactions. The oracle frontend at oracle.truesight.me calls this
    endpoint via fetch() and requires Access-Control-Allow-Origin: *."""
    return JSONResponse(content=content, status_code=status_code, headers=_CORS_HEADERS)


@app.api_route("/oracle-advisory", methods=["GET", "OPTIONS"])
async def oracle_advisory(
    request: Request,
    mode: str = "",
    signature: str = "",
    primary_number: str = "",
    primary_name: str = "",
    primary_judgment: str = "",
    related_number: str = "",
    related_name: str = "",
    related_judgment: str = "",
    changing_lines: str = "",
    timestamp_utc: str = "",
    qmdj_chart: str = "",
):
    """Public endpoint that replaces the GAS oracle_advisory_bridge script.

    Accepts the same GET params as the GAS script. Fetches the current
    ADVISORY_SNAPSHOT.md from GitHub, builds a system prompt instructing
    the LLM to act as a DAO Oracle interpreting the I Ching hexagram in
    the context of live DAO state, calls DeepSeek, and returns JSON in
    the same shape as the GAS bridge.

    Supports both GET and OPTIONS (CORS preflight) methods.
    """
    # CORS preflight — return 204 with CORS headers (no body)
    if request.method == "OPTIONS":
        from fastapi.responses import Response
        return Response(status_code=204, headers=_CORS_HEADERS)

    # Rate limit: 1 req per 10s per IP
    ip = request.client.host if request.client else "unknown"
    _check_oracle_rate_limit(ip)

    # 1. Fetch ADVISORY_SNAPSHOT.md from GitHub raw URL
    snapshot_url = "https://raw.githubusercontent.com/TrueSightDAO/dao_protocol/main/ADVISORY_SNAPSHOT.md"
    snapshot_text = ""
    try:
        import httpx
        resp = httpx.get(snapshot_url, timeout=15.0)
        if resp.status_code == 200:
            snapshot_text = resp.text
        else:
            logger.warning("oracle-advisory: failed to fetch snapshot (HTTP %d)", resp.status_code)
    except Exception as e:
        logger.warning("oracle-advisory: error fetching snapshot: %s", e)

    # 2. Build system prompt
    system_prompt = (
        "You are the TrueSight DAO Oracle — a wise, grounded interpreter of the I Ching "
        "for a real-world regenerative DAO. Your role is to read the current hexagram "
        "(primary + relating) and the DAO's live state snapshot, then produce a concise "
        "advisory that connects the ancient wisdom to the DAO's present situation.\n\n"
        "Speak in plain English. Be direct, practical, and honest. Do not sugarcoat. "
        "If the hexagram warns of danger, say so. If it signals opportunity, name it. "
        "Ground every insight in the DAO's actual metrics, treasury, and governance "
        "state from the snapshot below.\n\n"
        "Output format: a short paragraph (3–6 sentences) that a DAO governor can act on.\n\n"
        "---\n"
    )

    if snapshot_text:
        system_prompt += f"## DAO State Snapshot (ADVISORY_SNAPSHOT.md)\n\n{snapshot_text}\n\n"
    else:
        system_prompt += "## DAO State Snapshot\n\n(Unavailable — advisory based on hexagram alone.)\n\n"

    system_prompt += (
        "---\n"
        "## I Ching Reading\n\n"
        f"- **Mode**: {mode}\n"
        f"- **Primary Hexagram**: {primary_number} — {primary_name}\n"
        f"- **Primary Judgment**: {primary_judgment}\n"
    )
    if related_number:
        system_prompt += (
            f"- **Related Hexagram**: {related_number} — {related_name}\n"
            f"- **Related Judgment**: {related_judgment}\n"
        )
    if changing_lines:
        system_prompt += f"- **Changing Lines**: {changing_lines}\n"
    if qmdj_chart:
        system_prompt += f"- **QMDJ Chart**: {qmdj_chart}\n"
    if timestamp_utc:
        system_prompt += f"- **Reading Timestamp (UTC)**: {timestamp_utc}\n"

    system_prompt += (
        "\nNow produce your oracle advisory for the DAO. "
        "Be concise, grounded, and actionable."
    )

    # 3. Call DeepSeek via LLMClient
    client = LLMClient()
    user_msg = (
        f"The DAO has cast hexagram {primary_number} ({primary_name}) "
        f"with mode '{mode}'. Please provide the oracle advisory."
    )
    try:
        completion = client.chat(system_prompt, [{"role": "user", "content": user_msg}], tools=None)
        advice = client.extract_text(completion)
        model_used = client.model
    except LLMError as e:
        logger.error("oracle-advisory: LLM error: %s", e)
        return _cors_json_response(
            {"ok": False, "error": f"LLM call failed: {e}"},
            status_code=503,
        )

    # 4. Return JSON in the same shape as the GAS bridge
    from datetime import datetime, timezone
    return _cors_json_response({
        "ok": True,
        "advice": advice,
        "model": model_used,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ── Daily Briefing (Morning Oracle Standup) ──────────────────────────────


@app.api_route("/daily-briefing", methods=["POST", "OPTIONS"])
async def daily_briefing(request: Request):
    """POST /daily-briefing — triggered by the oracle after a governor's reading.

    Accepts a signed payload (same pattern as /chat-blocking):
      - X-Public-Key header: the governor's public key
      - JSON body with `payload` (dict) and `signature` (base64 string)

    The payload should contain:
      - reading: dict with primary_hexagram, related_hexagram, etc.
      - timestamp_utc: ISO 8601 timestamp of the reading

    Steps:
      1. Verify the signature maps to a governor
      2. Dedup per governor per day
      3. Compose agenda from live sources
      4. Post to Telegram #General
      5. Return JSON result

    Fire-and-forget from the oracle's perspective — the oracle POSTs and
    doesn't wait for a rendered page.
    """
    # CORS preflight
    if request.method == "OPTIONS":
        from fastapi.responses import Response
        return Response(status_code=204, headers=_CORS_HEADERS)

    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        return _cors_json_response({"ok": False, "error": "X-Public-Key header required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return _cors_json_response({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    payload = body.get("payload", {})
    signature = body.get("signature", "")

    if not payload or not signature:
        return _cors_json_response({"ok": False, "error": "payload and signature required"}, status_code=400)

    result = await handle_daily_briefing(payload, signature, public_key)
    status_code = 200 if result.get("ok") else (400 if result.get("error") else 200)
    return _cors_json_response(result, status_code=status_code)


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    """Serve uploaded files (images, PDFs, etc.) for thumbnail display."""
    file_path = UPLOAD_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404)
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(file_path, media_type=mime_type or "application/octet-stream")


# ───────────────────────────── Governor Chat ─────────────────────────────

@app.get("/session")
async def get_session(request: Request, limit: int = 30) -> JSONResponse:
    """Return the current conversation history so the frontend can restore it on refresh.
    Uses X-Public-Key + X-Session-Id headers (same as session keying)."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    session_id = _session_key(public_key, request)
    history = _load_or_create_session(session_id)

    # Only return last N messages for performance
    recent = history[-limit:] if len(history) > limit else history

    # Filter out internal system messages and strip legacy XML leaks
    visible = []
    for msg in recent:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        # Truncate very long messages (context files can be 10K+ chars)
        if len(content) > 2000:
            content = content[:2000] + "\n...(truncated)"
        # Strip legacy XML tool-call syntax if it leaked through
        content = re.sub(r'<function_calls>.*?</function_calls>', '', content, flags=re.DOTALL).strip()
        if not content:
            continue
        if role in ("user", "assistant") and "GOVERNOR_IDENTITY:" not in content:
            visible.append({"role": role, "content": content})
        elif role == "system" and ("[ROLE:" in content or "[PENDING_ROLE:" in content):
            visible.append({"role": "system", "content": content})

    return JSONResponse({"messages": visible, "session_id": session_id})


@app.post("/session/reset")
async def reset_session(request: Request) -> JSONResponse:
    """Overwrite session history (used by /reset command)."""
    public_key = verify_jwt(request)
    body = await request.json()
    messages = body.get("messages", [])
    session_id = _session_key(public_key, request)
    _sessions[session_id] = messages
    _log_session(session_id, messages)
    return JSONResponse({"status": "ok", "message_count": len(messages)})


@app.get("/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    """List all saved sessions for this governor."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    import hashlib
    idx_file = SESSION_LOG_DIR / f"{hashlib.md5(public_key.encode()).hexdigest()[:12]}_sessions.json"
    if not idx_file.exists():
        return JSONResponse({"sessions": []})
    data = json.loads(idx_file.read_text(encoding="utf-8"))
    return JSONResponse({"sessions": data.get("sessions", [])})


@app.get("/pending")
async def get_pending(request: Request) -> JSONResponse:
    """Return pending approvals for this governor (persistent, survives refresh).

    Performs a best-effort sweep of stale `merge_pr` entries (PRs the
    governor merged via GitHub directly without clicking Approve in the
    chat UI) before returning, so the chat panel stops showing items
    that are no longer actionable."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    try:
        _cleanup_resolved_pending(public_key)
    except Exception as e:
        logger.warning("Lazy pending cleanup failed: %s", e)
    pending = _load_pending(public_key)
    return JSONResponse({"pending": pending})


@app.post("/pending/resolve")
async def resolve_pending(request: Request) -> JSONResponse:
    """Mark a pending proposal as resolved (approved or rejected)."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    body = await request.json()
    qr_code = body.get("qr_code", "")
    action = body.get("action", "approved")
    _resolve_pending(public_key, qr_code, action)
    return JSONResponse({"status": "ok"})


@app.post("/pending/add")
async def add_pending(request: Request) -> JSONResponse:
    """Add a pending proposal (called by frontend when batch proposals are rendered)."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    body = await request.json()
    _add_pending(public_key, body)
    return JSONResponse({"status": "ok"})


@app.post("/sessions/new")
async def new_session(request: Request) -> JSONResponse:
    """Create a new empty session and return its ID."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    sid = str(uuid.uuid4())
    session_key = f"{public_key[:20]}:{sid}"
    _sessions[session_key] = []
    _save_session_index(public_key, sid)
    return JSONResponse({"session_id": sid})


@app.patch("/sessions/{sid}")
async def rename_session(sid: str, request: Request) -> JSONResponse:
    """Rename a session."""
    public_key = request.headers.get("X-Public-Key", "")
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    _save_session_index(public_key, sid, name)
    return JSONResponse({"status": "ok"})


@app.post("/auth/challenge")
async def auth_challenge(request: Request) -> JSONResponse:
    """Step 1: client sends signed payload; server verifies and returns JWT."""
    body = await request.json()
    payload = body.get("payload")
    signature = body.get("signature")
    public_key = request.headers.get("X-Public-Key", "")

    if not payload or not signature or not public_key:
        raise HTTPException(status_code=400, detail="payload, signature, and X-Public-Key required.")

    verify_payload(payload, signature, public_key)
    token = create_jwt(public_key)

    response = JSONResponse({"token": token, "expires_in": settings.jwt_expiry_minutes * 60})
    response.set_cookie(
        key="governor_chat_session",
        value=token,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        max_age=settings.jwt_expiry_minutes * 60,
    )
    return response


def _sse_event(event_type: str, data: object) -> str:
    return f"data: {json.dumps({'type': event_type, **({'content': data} if not isinstance(data, dict) else data)})}\n\n"


async def _sse_single_response(text: str):
    """Yield a single 'done' SSE event — used for non-LLM responses like role menus."""
    yield _sse_event("done", {"response": text})


async def _heartbeat_until_done(task: asyncio.Task, phase: str, session_id: str | None = None, **meta):
    """Async generator. Yields SSE `heartbeat` events every 15s while `task`
    is still pending. Caller awaits `task` after this generator exits to
    retrieve the result. Keeps the SSE connection alive across long-running
    LLM calls and tool calls (fixes ChunkedEncodingError on idle streams).

    If `session_id` is provided, also polls `_cancel_flags[session_id]` and
    cancels the inner task when the caller hits `DELETE /chat/active/{sid}`."""
    started = time.monotonic()
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - started, 1)
            if session_id and _cancel_flags.get(session_id):
                task.cancel()
                yield _sse_event("cancelled", {"phase": phase, "elapsed_s": elapsed, **meta})
                return
            yield _sse_event("heartbeat", {"phase": phase, "elapsed_s": elapsed, **meta})


_DSML_OPEN_TOKEN = "<｜｜DSML｜｜"  # raw DSML opener seen leaking from DeepSeek
_DSML_BLOCK_RE = re.compile(
    r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>",
    re.DOTALL,
)
_DSML_FRAGMENT_RE = re.compile(r"<｜｜DSML｜｜[^<]*?(?:>|$)")


def _strip_dsml(text: str) -> tuple[str, bool]:
    """Strip raw `<｜｜DSML｜｜…>` tool-call leakage from text.

    DeepSeek occasionally emits the DSML XML format as plain content tokens
    instead of as structured tool_calls (especially when the round budget is
    exhausted). The DSML strings then stream straight to the user, which is
    confusing and looks broken. This strips them and returns
    (sanitized_text, had_leakage). Caller can use `had_leakage` to emit a
    `wanted_more_rounds` signal."""
    if _DSML_OPEN_TOKEN not in text:
        return text, False
    sanitized = _DSML_BLOCK_RE.sub("", text)
    sanitized = _DSML_FRAGMENT_RE.sub("", sanitized)
    return sanitized.strip(), True


# Canonical labels from dao_client modules — each event type has exact field names.
# Source: truesight_dao_client/modules/{report_inventory_movement,report_sales,...}.py
_CANONICAL_LABELS: dict[str, list[str]] = {
    "INVENTORY MOVEMENT": [
        "Manager Name", "Recipient Name", "Inventory Item", "QR Code",
        "Quantity", "Latitude", "Longitude", "Attached Filename",
        "Destination Inventory File Location", "Submission Source",
    ],
    "SALES EVENT": [
        "Item", "Sales price", "Sold by", "Cash proceeds collected by",
        "Owner email", "Stripe Session ID", "Shipping Provider",
        "Tracking number", "Attached Filename", "Submission Source",
    ],
    "CONTRIBUTION EVENT": [
        "Type", "Amount", "Description", "Contributor(s)", "TDG Issued",
    ],
    "QR CODE EVENT": [
        "Attached Filename", "Submission Source",
    ],
    "QR CODE UPDATE EVENT": [],
    "CAPITAL INJECTION EVENT": [
        "Ledger", "Ledger URL", "Amount", "Description",
    ],
    "TREE PLANTING EVENT": [
        "Number of trees planted", "Species", "Location", "Attached Filename", "Submission Source",
    ],
    "DAO Inventory Expense Event": [
        "DAO Member Name", "Target Ledger", "Latitude", "Longitude",
        "Inventory Type", "Inventory Quantity", "Description",
        "Attached Filename", "Destination Inventory File Location",
    ],
}

# Map of LLM-invented field names → canonical labels (case-insensitive matching)
_FIELD_ALIASES: dict[str, str] = {
    # Inventory Movement
    "manager_name": "Manager Name", "manager": "Manager Name",
    "manager (from)": "Manager Name", "from": "Manager Name",
    "sender": "Manager Name", "source": "Manager Name",
    "recipient_name": "Recipient Name", "recipient": "Recipient Name",
    "recipient (to)": "Recipient Name", "to": "Recipient Name",
    "receiver": "Recipient Name", "destination_name": "Recipient Name",
    "inventory_item": "Inventory Item", "item": "Inventory Item",
    "product": "Inventory Item", "item_name": "Inventory Item",
    "qr_code": "QR Code", "qr": "QR Code", "code": "QR Code",
    "quantity": "Quantity", "qty": "Quantity", "count": "Quantity",
    "amount": "Quantity",
    "destination_inventory_file_location": "Destination Inventory File Location",
    "destination": "Destination Inventory File Location",
    "ledger": "Destination Inventory File Location",
    "ledger_name": "Destination Inventory File Location",
    "location": "Destination Inventory File Location",
    "attached_filename": "Attached Filename", "filename": "Attached Filename",
    "attachment": "Attached Filename",
    # Sales
    "sales_price": "Sales price", "price": "Sales price",
    "sold_by": "Sold by",
    "cash_proceeds_collected_by": "Cash proceeds collected by",
    "owner_email": "Owner email", "email": "Owner email",
    "stripe_session_id": "Stripe Session ID", "stripe": "Stripe Session ID",
    "shipping_provider": "Shipping Provider",
    "tracking_number": "Tracking number", "tracking": "Tracking number",
    # Contribution
    "contributor": "Contributor(s)", "contributors": "Contributor(s)",
    "contributor(s)": "Contributor(s)",
    "tdg_issued": "TDG Issued", "tdgs": "TDG Issued",
    # General
    "submission_source": "Submission Source",
}

# Descriptive-only fields that should be dropped (not canonical labels)
_NON_CANONICAL_KEYS = {
    "type", "description", "notes", "status", "summary",
    "details", "comment", "remarks", "tags",
}


def _normalize_submission_labels(event_name: str, attributes: dict) -> dict:
    """Coerce LLM-generated attribute keys to canonical dao_client labels.
    
    Steps:
    1. Map alias names to canonical labels
    2. Drop non-canonical descriptive keys
    3. Keep only canonical labels for the event type
    """
    canonical_set = set(_CANONICAL_LABELS.get(event_name, []))

    normalized = {}
    for key, value in attributes.items():
        # Skip descriptive-only keys
        if key.lower() in _NON_CANONICAL_KEYS:
            continue
        # Map aliases to canonical names
        canonical_key = _FIELD_ALIASES.get(key.lower(), key)
        # If event has defined canonical labels, only keep matching ones
        if canonical_set and canonical_key not in canonical_set:
            # For events with no defined labels (QR CODE UPDATE), keep all
            if canonical_set:
                continue
        normalized[canonical_key] = str(value)

    return normalized


def _validate_required_fields(event_name: str, attributes: dict) -> list[str]:
    """Return list of missing required fields. Empty list = valid."""
    required = {
        "INVENTORY MOVEMENT": ["Manager Name", "Recipient Name", "QR Code"],
        "SALES EVENT": ["Item", "Sales price", "Sold by"],
        "CONTRIBUTION EVENT": ["Type", "Amount"],
        "CAPITAL INJECTION EVENT": ["Ledger", "Amount"],
    }
    missing = []
    for field in required.get(event_name, []):
        if field not in attributes:
            missing.append(field)
    return missing


async def _run_tool(func_name: str, func_args: dict, history: list[dict] | None = None, session_id: str | None = None, governor_name: str | None = None) -> str:
    # First try the capability-manifest registry. Tools whose TOOL_SPEC carries
    # a handler (the ~30 simple wrappers + the new google/gmail/aws/pdf tools)
    # dispatch here and we never reach the legacy if-branches below.
    # Orchestration tools (submit_contribution, create_dao_submission, open_fix_pr)
    # have handler=None in their spec and fall through to the inline branches.
    from .tool_registry import dispatch as _registry_dispatch
    _registry_result = _registry_dispatch(
        func_name, func_args or {},
        {"history": history or [], "session_id": session_id, "governor_name": governor_name},
    )
    if _registry_result is not None:
        return _registry_result
    if func_name == "list_org_repos":
        gh = GitHubClient()
        repos = gh.list_org_repos()
        if repos:
            lines = [f"- {r['name']} ({'private' if r['private'] else 'public'}) — {r['description']}" for r in repos]
            return "TrueSightDAO repositories:\n" + "\n".join(lines)
        return "Failed to list repos or none found."
    if func_name == "read_context_file":
        result = get_context_file(func_args.get("path", ""))
        return result if result else "File not found."
    if func_name == "read_repo_file":
        result = read_repo_file(
            func_args.get("repo", ""),
            func_args.get("path", ""),
            func_args.get("ref", "main"),
        )
        if result.get("type") == "file":
            return result["content"]
        if result.get("type") == "directory":
            return "Directory listing:\n" + "\n".join(
                f"- {e['name']} ({e['type']})" for e in result.get("entries", [])
            )
        return f"Error: {result.get('error', 'unknown')}"
    if func_name == "submit_contribution":
        # DUPLICATE GUARD: check DAO ledger (ground truth) before submitting
        event_name = func_args.get("event_name", "CONTRIBUTION EVENT")
        attributes = func_args.get("attributes", {})

        # Normalize attribute keys to canonical labels
        attributes = _normalize_submission_labels(event_name, attributes)

        # Validate required fields
        missing = _validate_required_fields(event_name, attributes)
        if missing:
            return json.dumps({
                "status": "invalid",
                "message": f"Missing required fields for {event_name}: {', '.join(missing)}. Canonical labels are: {', '.join(_CANONICAL_LABELS.get(event_name, ['(any)']))}",
            })

        qr = attributes.get("QR Code", "")
        recipient = attributes.get("Recipient Name", "")

        # 1. Check session history for prior submission
        if qr and history:
            for msg in history:
                content = str(msg.get("content", ""))
                if msg.get("role") == "tool" and qr in content and ("submitted successfully" in content.lower() or "duplicate" in content.lower()):
                    return json.dumps({"status": "duplicate", "message": f"QR code {qr} was already submitted. Skipping."})

        # 2. Check DAO ledger (ground truth)
        if qr:
            try:
                ledger_state = lookup_qr_code(qr)
                if ledger_state.get("status") == "success":
                    current_status = ledger_state.get("qr_status", "").upper()
                    current_manager = ledger_state.get("manager_name", "")
                    if current_status not in ("MINTED", ""):
                        return json.dumps({
                            "status": "duplicate",
                            "message": f"QR code {qr} has status '{current_status}' (manager: {current_manager}) — already processed. Ground truth from DAO ledger.",
                            "ledger_state": ledger_state,
                        })
                    if recipient and current_manager and recipient.lower() == current_manager.lower():
                        return json.dumps({
                            "status": "duplicate",
                            "message": f"QR code {qr} is already managed by {current_manager} (recipient is also {recipient}). Nothing to move.",
                            "ledger_state": ledger_state,
                        })
            except Exception:
                pass

        # 3. APPROVAL GATE: check if the most recent user message explicitly approved this submission
        approved = False
        if history:
            for msg in reversed(history):
                if msg.get("role") == "user":
                    content = str(msg.get("content", "")).lower()
                    clean_content = content.replace("[governor_identity:", "").split("\n## instructions\n")[0]
                    if qr and qr.lower() in clean_content:
                        if any(kw in clean_content for kw in ["approved", "approve", "go ahead", "yes", "confirm", "proceed", "execute", "do it", "submit it"]):
                            approved = True
                        elif any(kw in clean_content for kw in ["reject", "cancel", "no", "stop", "don't"]):
                            return json.dumps({"status": "cancelled", "message": "Submission cancelled by user."})
                    break  # Only check the single most recent user message

        if not approved:
            # Build proposal for frontend to render as Approve/Reject card
            manager = attributes.get("Manager Name", "")
            item = attributes.get("Inventory Item", "")
            qty = attributes.get("Quantity", "1")
            ledger = attributes.get("Destination Inventory File Location", "")
            summary = f"Move {qty}x {item} from {manager} to {recipient}" if item else f"Submit {event_name} for {qr}"
            command = f"truesight-dao-report-inventory-movement"
            if manager: command += f' --manager-name "{manager}"'
            if recipient: command += f' --recipient-name "{recipient}"'
            if item: command += f' --inventory-item "{item}"'
            if qr: command += f' --qr-code "{qr}"'
            if qty: command += f' --quantity "{qty}"'
            if ledger: command += f' --destination-inventory-file-location "{ledger}"'

            proposal = {
                "status": "pending_approval",
                "proposal": {
                    "action": "submit_contribution",
                    "title": f"{event_name}: {qr}" if qr else event_name,
                    "summary": summary,
                    "command": command,
                    "tool_args": {"event_name": event_name, "attributes": attributes},
                },
                "message": f"⏳ Waiting for your approval to submit this transaction. Click Approve to proceed, or Reject to cancel."
            }

            # Persist pending approval to server + GitHub for durability
            if qr:
                pub_key = session_key.split(":")[0] if hasattr(session_key, 'split') else ""
                if pub_key:
                    _add_pending(pub_key, {
                        "title": f"{event_name}: {qr}" if qr else event_name,
                        "qr_code": qr,
                        "summary": summary,
                        "action": "submit_contribution",
                    })

            return json.dumps(proposal)

        # APPROVED — execute
        # Add agentic traceability: who approved this, with proof of their authenticated session
        if governor_name and event_name == "INVENTORY MOVEMENT":
            import hashlib
            key_seed = (session_id or "").split(":")[0]
            fingerprint = hashlib.sha256(key_seed.encode()).hexdigest()[:8]
            today = time.strftime("%Y-%m-%d", time.gmtime())
            sid_hash = hashlib.md5((session_id or "").encode()).hexdigest()[:12]
            transcript_url = f"https://github.com/TrueSightDAO/{_TRANSCRIPT_REPO}/blob/main/sessions/{today}/{sid_hash}/transcript.md"
            attributes["Approved By"] = f"{governor_name} | Key FP: {fingerprint} | Session: {transcript_url}"

        edgar = EdgarDirectClient()
        ok = edgar.submit_contribution(event_name, attributes, description=attributes.get("Description", ""))
        return "Contribution submitted successfully." if ok else "Failed to submit contribution."
    if func_name == "open_fix_pr":
        from .fix_agent import repo_class_block
        repo_name = func_args.get("repo", "")
        issue = func_args.get("issue_description", "")
        allowed = settings.allowed_repos
        if repo_name not in allowed:
            return f"Error: repo '{repo_name}' not in allowed list."
        blocked = repo_class_block(repo_name)
        if blocked:
            return blocked
        fixer = FixAgent()
        pr_url = fixer.run_simple(repo_name, issue)
        if not pr_url:
            return "Fix agent failed to produce a PR."
        # Extract PR number from URL like https://github.com/TrueSightDAO/dapp/pull/218
        import re as _re
        m = _re.search(r"/pull/(\d+)$", pr_url)
        pr_number = int(m.group(1)) if m else 0
        # Build a merge_pr proposal so the frontend renders an Approve/Reject card
        proposal = {
            "proposal": {
                "action": "merge_pr",
                "title": f"Merge PR #{pr_number} on {repo_name}",
                "pr_number": pr_number,
                "repo": repo_name,
                "summary": issue[:200],
            }
        }
        # Persist pending approval so it shows in the hamburger menu
        if session_id:
            pub_key = session_id.split(":")[0]
            if pub_key:
                _add_pending(pub_key, {
                    "title": f"Merge PR #{pr_number} on {repo_name}",
                    "qr_code": "",
                    "summary": issue[:200],
                    "action": "merge_pr",
                })
        return f"PR opened: {pr_url}\n\n```json\n{json.dumps(proposal)}\n```"
    if func_name == "merge_pr":
        repo_name = func_args.get("repo", "")
        pr_number = func_args.get("pr_number", 0)
        merge_method = func_args.get("merge_method", "squash")
        allowed = settings.allowed_repos
        if repo_name not in allowed:
            return f"Error: repo '{repo_name}' not in allowed list."
        if not pr_number:
            return "Error: pr_number is required."
        gh = GitHubClient()
        result = gh.merge_pr(repo_name, int(pr_number), merge_method)
        if result["merged"]:
            return f"✅ PR #{pr_number} on {repo_name} merged successfully (sha: {result['sha']}). {result['message']}"
        return f"❌ Failed to merge PR #{pr_number} on {repo_name}: {result['message']}"
    if func_name == "scan_qr_from_file":
        file_path = func_args.get("file_path", "")
        result = scan_qr_from_file(file_path)
        return json.dumps(result, indent=2)
    if func_name == "scan_qr_batch":
        file_paths = func_args.get("file_paths", [])
        result = scan_qr_batch(file_paths)
        return json.dumps(result, indent=2)
    if func_name == "lookup_qr_code":
        qr_code = func_args.get("qr_code", "")
        result = lookup_qr_code(qr_code)
        return json.dumps(result, indent=2)
    if func_name == "lookup_qr_batch":
        qr_codes = func_args.get("qr_codes", [])
        result = lookup_qr_batch(qr_codes)
        return json.dumps(result, indent=2)
    if func_name == "list_matching_qr_codes":
        prefix = func_args.get("prefix", "")
        if not prefix:
            return json.dumps({"status": "error", "message": "prefix is required"})
        result = list_matching_qr_codes(prefix)
        return json.dumps(result, indent=2)
    if func_name == "register_identity":
        email = func_args.get("email", "")
        if not email:
            return json.dumps({"success": False, "error": "email is required"})
        result = register_identity(email)
        return json.dumps(result, indent=2)
    if func_name == "list_prs":
        repo = func_args.get("repo", "")
        state = func_args.get("state", "all")
        limit = int(func_args.get("limit", 20))
        if not repo:
            return json.dumps({"status": "error", "message": "repo is required"})
        gh = GitHubClient()
        prs = gh.list_prs(repo, state=state, limit=limit)
        return json.dumps(prs, indent=2)
    if func_name == "read_oracle_logs":
        from .tools.read_oracle_logs import read_oracle_logs as _read_logs
        date = func_args.get("date", "latest")
        return _read_logs(date)
    if func_name == "create_dao_submission":
        title = func_args.get("title", "")
        body = func_args.get("body", "")
        pr_urls = func_args.get("pr_urls", [])
        contributors = func_args.get("contributors", governor_name or "autopilot@agroverse.shop")
        amount = func_args.get("amount", "0")
        tdg_issued = func_args.get("tdg_issued", "0")
        attachment_path = func_args.get("attachment_path", "")
        attachment_filename = func_args.get("attachment_filename", "")
        if not title or not body or not pr_urls:
            return json.dumps({"status": "error", "message": "title, body, and pr_urls are required"})
        edgar = EdgarDirectClient()
        if not edgar.is_configured():
            return json.dumps({"status": "error", "message": "Edgar credentials not configured — cannot submit"})
        pr_block = "Pull requests (GitHub evidence):\n" + "\n".join(f"- {u.strip()}" for u in pr_urls)
        description = f"{title}\n\n{pr_block}\n\nDetails:\n{body}"
        attrs: dict[str, str] = {
            "Type": "Time (Minutes)" if amount == "0" or float(amount) > 60 else "USD",
            "Amount": amount,
            "Description": description,
            "Contributor(s)": contributors,
            "TDG Issued": tdg_issued,
        }
        if attachment_path and os.path.isfile(attachment_path):
            from .tools.dao_submission import submit_ai_agent_contribution as _submit_ai
            result = _submit_ai(
                title=title, body=body, pr_urls=pr_urls,
                contributors=contributors, amount=amount, tdg_issued=tdg_issued,
                attached_file_path=attachment_path,
                attached_filename=attachment_filename or None,
            )
            return json.dumps({"status": "success" if result.get("status") == "success" else "error",
                               "message": "Contribution with attachment submitted" if result.get("status") == "success" else f"Submission failed: {result.get('stderr', '')}"})
        else:
            attrs["Attached Filename"] = "N/A"
            attrs["Destination Contribution File Location"] = "N/A"
            ok = edgar.submit_contribution("CONTRIBUTION EVENT", attrs, description=title)
            return json.dumps({"status": "success" if ok else "error", "message": "Contribution submitted" if ok else "Submission failed"})
    if func_name == "upload_file_to_github":
        from .tools.upload_file_to_github import upload_file_to_github as _upload
        result = _upload(
            repo=func_args.get("repo", ""),
            path=func_args.get("path", ""),
            content=func_args.get("content", ""),
            message=str(func_args.get("message", "Upload via autopilot"))[:72],
            branch=func_args.get("branch", "main"),
        )
        if result.get("status") == "success":
            blob_url = f"https://github.com/TrueSightDAO/{func_args.get('repo', '')}/blob/{func_args.get('branch', 'main')}/{func_args.get('path', '')}"
            return json.dumps({"status": "success", "blob_url": blob_url, "commit_sha": result.get("commit_sha", ""), "message": result.get("message", "")})
        return json.dumps(result)
    if func_name == "deploy_autopilot":
        from .tools.deploy import deploy_autopilot as _deploy
        return _deploy()
    if func_name == "list_directory":
        dir_path = func_args.get("dir_path", "")
        result = list_directory(dir_path)
        return json.dumps(result, indent=2)
    if func_name == "read_local_file":
        file_path = func_args.get("file_path", "")
        result = read_local_file(file_path)
        return json.dumps(result, indent=2)
    if func_name == "web_search":
        from .tools.web_search import web_search as _web_search
        return _web_search(
            query=func_args.get("query", ""),
            max_results=func_args.get("max_results", 5),
            search_depth=func_args.get("search_depth", "basic"),
            include_answer=func_args.get("include_answer", True),
        )
    if func_name == "web_extract":
        from .tools.web_search import web_extract as _web_extract
        return _web_extract(urls=func_args.get("urls", []))
    if func_name == "read_google_sheet":
        from .tools.google_sheets import read_google_sheet as _read_google_sheet
        return _read_google_sheet(
            spreadsheet_id=func_args.get("spreadsheet_id", ""),
            range_a1=func_args.get("range_a1", ""),
            service_account_name=func_args.get("service_account_name"),
        )
    if func_name == "read_google_doc":
        from .tools.google_docs import read_google_doc as _read_google_doc
        return _read_google_doc(
            document_id=func_args.get("document_id", ""),
            service_account_name=func_args.get("service_account_name"),
        )
    if func_name == "read_drive_file":
        from .tools.google_drive import read_drive_file as _read_drive_file
        return _read_drive_file(
            file_id=func_args.get("file_id", ""),
            mime_type=func_args.get("mime_type"),
            service_account_name=func_args.get("service_account_name"),
        )
    if func_name == "list_drive_folder":
        from .tools.google_drive import list_drive_folder as _list_drive_folder
        return _list_drive_folder(
            folder_id=func_args.get("folder_id", ""),
            page_size=func_args.get("page_size", 50),
            service_account_name=func_args.get("service_account_name"),
        )
    if func_name == "http_fetch":
        from .tools.http_fetch import http_fetch as _http_fetch
        return _http_fetch(
            url=func_args.get("url", ""),
            method=func_args.get("method", "GET"),
            body=func_args.get("body"),
            headers=func_args.get("headers"),
            timeout=func_args.get("timeout"),
        )
    if func_name == "aws_query":
        from .tools.aws_tools import aws_query as _aws_query
        return _aws_query(
            account=func_args.get("account", ""),
            service=func_args.get("service", ""),
            operation=func_args.get("operation", ""),
            parameters=func_args.get("parameters"),
            region=func_args.get("region"),
        )
    if func_name == "generate_pdf":
        from .tools.pdf_tools import generate_pdf as _generate_pdf
        return _generate_pdf(
            content=func_args.get("content", ""),
            title=func_args.get("title"),
            output_path=func_args.get("output_path"),
        )
    if func_name == "gmail_search":
        from .tools.gmail_tools import gmail_search as _gmail_search
        return _gmail_search(
            query=func_args.get("query", ""),
            account=func_args.get("account"),
            max_results=func_args.get("max_results", 20),
        )
    if func_name == "gmail_read_message":
        from .tools.gmail_tools import gmail_read_message as _gmail_read_message
        return _gmail_read_message(
            message_id=func_args.get("message_id", ""),
            account=func_args.get("account"),
        )
    if func_name == "gmail_send":
        from .tools.gmail_tools import gmail_send as _gmail_send
        return _gmail_send(
            to=func_args.get("to", ""),
            subject=func_args.get("subject", ""),
            body=func_args.get("body", ""),
            account=func_args.get("account"),
            cc=func_args.get("cc"),
            bcc=func_args.get("bcc"),
            attachment_path=func_args.get("attachment_path"),
        )
    if func_name == "gmail_create_draft":
        from .tools.gmail_tools import gmail_create_draft as _gmail_create_draft
        return _gmail_create_draft(
            to=func_args.get("to", ""),
            subject=func_args.get("subject", ""),
            body=func_args.get("body", ""),
            account=func_args.get("account"),
            cc=func_args.get("cc"),
            bcc=func_args.get("bcc"),
            attachment_path=func_args.get("attachment_path"),
        )
    if func_name == "gmail_list_labels":
        from .tools.gmail_tools import gmail_list_labels as _gmail_list_labels
        return _gmail_list_labels(account=func_args.get("account"))
    if func_name == "gmail_apply_label":
        from .tools.gmail_tools import gmail_apply_label as _gmail_apply_label
        return _gmail_apply_label(
            message_id=func_args.get("message_id", ""),
            add_labels=func_args.get("add_labels") or [],
            remove_labels=func_args.get("remove_labels") or [],
            account=func_args.get("account"),
        )
    return f"Unknown tool: {func_name}"


# Tools that change shared state (PRs, deploys, ledger, infra). A turn that runs
# any of these ends with an explicit "what I did" report so that — especially when
# several instructions are queued for one topic and run as back-to-back turns —
# the governor sees exactly what each turn accomplished before the next begins.
# See agentic_ai_context/SOPHIA_THREAD_CONCURRENCY_PLAN.md (PR3, invariant 7).
_SIDE_EFFECT_TOOLS = {
    "open_fix_pr", "merge_pr", "deploy_autopilot", "submit_contribution",
    "create_dao_submission", "upload_file_to_github", "register_identity",
    "ssh_run", "run_command", "deploy_gas_project", "gas_deploy_project",
    "create_branch", "commit_and_push", "open_pr", "append_to_transcript",
}


def _summarise_tool_result(result: str) -> str:
    """One-line salient detail from a tool result — prefer a URL (PR/deploy), else
    the first non-empty line."""
    text = result or ""
    urls = re.findall(r"https?://[^\s\"')]+", text)
    if urls:
        return urls[0]
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _build_turn_report(tool_trace: list[dict]) -> str:
    """Markdown footer enumerating the side-effecting actions this turn took.
    Returns '' when the turn only read/searched (nothing worth reporting)."""
    effects = [t for t in (tool_trace or []) if t.get("name") in _SIDE_EFFECT_TOOLS]
    if not effects:
        return ""
    lines = ["", "———", "**✅ Done this turn — actions taken:**"]
    for t in effects:
        label = str(t.get("name", "")).replace("_", " ")
        detail = _summarise_tool_result(t.get("result", ""))
        lines.append(f"• `{label}`" + (f" → {detail}" if detail else ""))
    return "\n".join(lines)


def _append_turn_report(assistant_text: str, state: dict) -> str:
    """Append the actions-taken report to a turn's response, if any."""
    report = _build_turn_report(state.get("tool_trace", []))
    if not report:
        return assistant_text
    return (assistant_text or "").rstrip() + "\n" + report


async def _run_tool_round_loop(
    *,
    client: LLMClient,
    system_prompt: str,
    tools: list,
    history: list[dict],
    session_id: str,
    governor_name: str | None,
    req_id: int,
    state: dict,
    queue_msg_id: str | None = None,
):
    """One conversational turn: runs the LLM ↔ tool-call loop up to
    MAX_TOOL_ROUNDS, yielding SSE event strings as it goes.

    Handles: cancel checks, mid-round queue interjection (when not in queue
    mode itself), heartbeat-wrapped LLM and tool calls, DSML sanitization,
    `wanted_more_rounds` signal.

    Writes `assistant_text`, `wanted_more_rounds`, and `cancelled` into
    `state` so the caller can pick them up after iterating. Yielded values
    are SSE event strings (already framed `data: …\\n\\n`).

    `queue_msg_id` flips the labels and includes the ID in SSE events
    when this turn is processing a queued (interjected-after-done) message.
    """
    MAX_TOOL_ROUNDS = int(os.getenv("CHAT_MAX_TOOL_ROUNDS", "30"))
    assistant_text = ""
    round_num = 0
    state["cancelled"] = False
    state["wanted_more_rounds"] = False
    state.setdefault("tool_trace", [])  # [{name, result}] for the per-turn report

    log_prefix = "QUEUE " if queue_msg_id else ""
    raw_label_prefix = "queue-" if queue_msg_id else ""

    def _emit(payload_extra: dict | None = None) -> dict:
        d = dict(payload_extra or {})
        if queue_msg_id:
            d["queue_msg_id"] = queue_msg_id
        return d

    while round_num < MAX_TOOL_ROUNDS:
        round_num += 1

        if _cancel_flags.get(session_id):
            logger.info("[%d] %sCancelled by user before round %d", req_id, log_prefix, round_num)
            yield _sse_event("cancelled", _emit({"round": round_num, "reason": "user_requested"}))
            state["cancelled"] = True
            state["assistant_text"] = assistant_text
            return

        # Mid-round interjection — only meaningful for the initial turn.
        # The queue-processing turn already started from a queued message, so
        # additional queued messages should accumulate behind it (handled by
        # the caller's outer queue drain).
        if not queue_msg_id:
            pending = _message_queues.get(session_id, [])
            while pending:
                next_msg = pending.pop(0)
                logger.info("[%d] Mid-round interjection: %s", req_id, next_msg["id"])
                yield _sse_event("queue", {"msg_id": next_msg["id"], "status": "interjected"})
                history.append({"role": "user", "content": next_msg["content"]})

        chat_task = asyncio.create_task(asyncio.to_thread(client.chat, system_prompt, history, tools=tools))
        try:
            async for hb in _heartbeat_until_done(chat_task, phase="llm", session_id=session_id, round=round_num, **({"queue_msg_id": queue_msg_id} if queue_msg_id else {})):
                yield hb
            completion = await chat_task
        except asyncio.CancelledError:
            logger.info("[%d] %sLLM call cancelled by user", req_id, log_prefix)
            yield _sse_event("cancelled", _emit({"phase": "llm", "round": round_num, "reason": "user_requested"}))
            state["cancelled"] = True
            state["assistant_text"] = assistant_text
            return
        _log_raw_llm(session_id, f"llm-{raw_label_prefix}round-{round_num}", completion["choices"][0] if "choices" in completion else completion)
        assistant_message = completion["choices"][0].get("message", {})
        tool_calls = client.extract_tool_calls(completion)
        logger.info("[%d] %sLLM RESP round=%d tools=%d tokens=%s", req_id, log_prefix, round_num,
                     len(tool_calls),
                     completion.get("usage", {}).get("total_tokens", "?"))

        tool_calls = assistant_message.get("tool_calls", [])

        if tool_calls:
            thought = assistant_message.get("content", "") or "Thinking..."
            yield _sse_event("token", thought)

            history.append({
                "role": "assistant",
                "content": assistant_message.get("content", ""),
                "reasoning_content": assistant_message.get("reasoning_content", ""),
                "tool_calls": [
                    {"id": tc["id"], "type": tc["type"],
                     "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    func_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    logger.warning("[%d] %sTOOL ARGS INVALID for %s: %s", req_id, log_prefix, func_name, raw_args[:200])
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({"error": "invalid_arguments", "raw": raw_args[:500]}),
                    })
                    continue
                tool_call_id = tc["id"]
                logger.info("[%d] %sTOOL CALL: %s args=%.200s", req_id, log_prefix, func_name, json.dumps(func_args))

                yield _sse_event("tool", {"tool": func_name, "status": "calling"})
                tool_task = asyncio.create_task(_run_tool(func_name, func_args, history, session_id, governor_name))
                try:
                    async for hb in _heartbeat_until_done(tool_task, phase="tool", session_id=session_id, tool=func_name, **({"queue_msg_id": queue_msg_id} if queue_msg_id else {})):
                        yield hb
                    result_text = await tool_task
                except asyncio.CancelledError:
                    logger.info("[%d] %sTool %s cancelled by user", req_id, log_prefix, func_name)
                    yield _sse_event("cancelled", _emit({"phase": "tool", "tool": func_name, "reason": "user_requested"}))
                    state["cancelled"] = True
                    state["assistant_text"] = assistant_text
                    return
                yield _sse_event("tool", {"tool": func_name, "status": "done"})
                logger.info("[%d] %sTOOL RESULT: %s result=%.300s", req_id, log_prefix, func_name, result_text[:300])

                state["tool_trace"].append({"name": func_name, "result": result_text})
                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                })
        else:
            assistant_text = client.extract_text(completion)
            break  # no more tool calls — exit loop

    if not assistant_text:
        logger.info("[%d] %sEmpty after %d tool rounds — forcing text-only completion", req_id, log_prefix, round_num)
        state["wanted_more_rounds"] = True
        completion = client.chat(system_prompt, history, tools=None)
        assistant_text = client.extract_text(completion)

    assistant_text, dsml_leaked = _strip_dsml(assistant_text)
    if dsml_leaked:
        state["wanted_more_rounds"] = True
        logger.info("[%d] %sStripped DSML leakage from final response", req_id, log_prefix)
    if state["wanted_more_rounds"]:
        yield _sse_event("wanted_more_rounds", _emit({"rounds_used": round_num, "round_cap": MAX_TOOL_ROUNDS}))
    state["assistant_text"] = assistant_text


async def _stream_chat(user_message: str, history: list[dict], session_id: str,
                       attachment_info: dict | None = None, governor_name: str | None = None,
                       do_not_publish: bool = False, role=None):
    system_prompt = get_system_prompt_for_role(role)
    client = LLMClient()
    tools = get_tool_schemas_for_role(role)
    req_id = int(time.time() * 1000) % 1000000
    logger.info("[%d] CHAT REQ: session=%s msg=%.150s attach=%s", req_id, session_id[:16], user_message, attachment_info is not None)

    # Trim history to avoid context overflow (max ~400K chars = ~100K tokens for messages)
    # Preserve role tags and other system messages at position 0
    MAX_HISTORY_CHARS = 400_000
    total = sum(len(msg.get("content", "")) for msg in history)
    while total > MAX_HISTORY_CHARS and len(history) > 4:
        i = 0
        while i < len(history) and history[i].get("role") == "system":
            i += 1
        if i >= len(history) - 1:
            break
        removed = history.pop(i)
        total -= len(removed.get("content", ""))

    # Sanitise orphaned tool messages (DeepSeek rejects them)
    _sanitise_tool_messages(history)

    try:
        state: dict = {}
        async for ev in _run_tool_round_loop(
            client=client, system_prompt=system_prompt, tools=tools,
            history=history, session_id=session_id, governor_name=governor_name,
            req_id=req_id, state=state,
        ):
            yield ev
        if state.get("cancelled"):
            return
        assistant_text = _append_turn_report(state.get("assistant_text", ""), state)

    except LLMError as exc:
        logger.error("[%d] CHAT ERROR: %s", req_id, exc)
        _record_chat_error(str(exc))
        yield _sse_event("error", str(exc))
        return

    # Log final response
    logger.info("[%d] CHAT RESP: len=%d tokens=%.150s", req_id, len(assistant_text), assistant_text[:150])

    # Parse embedded proposal JSON — supports both single and batch proposals
    proposal = None
    proposals = None  # batch mode
    try:
        json_match = re.search(r"```json\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```", assistant_text, re.DOTALL)
        if json_match:
            embedded = json.loads(json_match.group(1))
            if isinstance(embedded, list):
                proposals = embedded  # batch of proposals
            elif isinstance(embedded, dict) and "proposal" in embedded:
                proposal = embedded["proposal"]
            elif isinstance(embedded, dict) and "proposals" in embedded:
                proposals = embedded["proposals"]
            assistant_text = re.sub(r"```json\s*[\[\{][\s\S]*?[\]\}]\s*```", "", assistant_text, flags=re.DOTALL).strip()
    except Exception:
        pass

    # Stream final response tokens
    for chunk in _chunk_text(assistant_text):
        yield _sse_event("token", chunk)

    # Stream done event
    done_data: dict[str, object] = {"response": assistant_text}
    if proposals:
        done_data["proposals"] = proposals
    elif proposal:
        done_data["proposal"] = proposal
    yield f"data: {json.dumps({'type': 'done', **done_data})}\n\n"

    # Persist assistant response to session history
    history.append({"role": "assistant", "content": assistant_text})
    _sessions[session_id] = history
    _log_session(session_id, history)

    # Publish transcript to public GitHub repo for DAO transparency
    asyncio.create_task(_publish_transcript(session_id, history, governor_name, do_not_publish=do_not_publish))

    # ── Queue processing: after done, check for queued messages ──
    queue = _message_queues.get(session_id, [])
    while queue:
        next_msg = queue.pop(0)
        logger.info("[%d] Processing queued message: %s", req_id, next_msg["id"])
        yield _sse_event("queue", {"msg_id": next_msg["id"], "status": "processing"})

        queued_content = next_msg["content"]
        if governor_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
            queued_content = f"[GOVERNOR_IDENTITY: You are speaking with {governor_name}. When they say 'I', 'me', or 'my', they mean {governor_name}.]\n\n{queued_content}"
        history.append({"role": "user", "content": queued_content})
        _log_session(session_id, history)

        try:
            q_state: dict = {}
            async for ev in _run_tool_round_loop(
                client=client, system_prompt=system_prompt, tools=tools,
                history=history, session_id=session_id, governor_name=governor_name,
                req_id=req_id, state=q_state, queue_msg_id=next_msg["id"],
            ):
                yield ev
            if q_state.get("cancelled"):
                return
            queued_text = _append_turn_report(q_state.get("assistant_text", ""), q_state)
        except LLMError as exc:
            logger.error("[%d] QUEUE CHAT ERROR: %s", req_id, exc)
            _record_chat_error(str(exc))
            yield _sse_event("error", str(exc))
            break

        for chunk in _chunk_text(queued_text):
            yield _sse_event("token", chunk)
        done_data = {"response": queued_text, "queued": True, "msg_id": next_msg["id"]}
        yield f"data: {json.dumps({'type': 'done', **done_data})}\n\n"

        history.append({"role": "assistant", "content": queued_text})
        _sessions[session_id] = history
        _log_session(session_id, history)
        asyncio.create_task(_publish_transcript(session_id, history, governor_name, do_not_publish=do_not_publish))


def _chunk_text(text: str, size: int = 80) -> list[str]:
    """Split text into chunks for streaming, keeping newlines."""
    if not text:
        return []
    chunks = []
    for paragraph in text.split("\n"):
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        chunks.append(paragraph)
    return chunks


def _log_session(session_id: str, history: list[dict]) -> None:
    """Write FULL session history to disk for debugging broken conversations."""
    # Keep the in-memory cache in sync with what we persist. Branches that
    # *reassign* history (build_role_menu, archive_old_history, pending-filter)
    # would otherwise leave _sessions[session_id] pointing at the old list, so
    # _load_or_create_session returns stale empty history and the role menu
    # re-prompts forever (replying "1" never sticks).
    _sessions[session_id] = history
    try:
        import hashlib
        sid_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
        log_path = SESSION_LOG_DIR / f"{sid_hash}.json"
        log_data = {
            "session_hash": sid_hash,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "message_count": len(history),
            "full_history": history,
        }
        # Atomic write: a torn/clobbered transcript is what bricks a thread, so
        # write to a temp file in the same dir and os.replace() it into place.
        tmp_path = log_path.with_name(f"{sid_hash}.json.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, log_path)
        # Ease-of-access: symlink/pointer to latest session
        latest = SESSION_LOG_DIR / "_latest.json"
        latest.write_text(json.dumps({"session_hash": sid_hash, "updated_at": log_data["updated_at"], "message_count": log_data["message_count"]}))
    except Exception:
        pass


def _log_raw_llm(session_id: str, label: str, payload: object) -> None:
    """Log raw LLM request/response for post-mortem debugging (XML leaks, QR misreads, etc)."""
    try:
        import hashlib
        sid_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
        debug_log = SESSION_LOG_DIR / f"{sid_hash}_debug.log"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        entry = f"\n=== {ts} {label} ===\n{json.dumps(payload, indent=2, ensure_ascii=False, default=str)}\n"
        with open(debug_log, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def _save_session_index(user_key: str, sid: str, name: str | None = None) -> None:
    """Update the session index file for a user. user_key should be a contributor name."""
    import hashlib
    idx_key = _gov_name_for_key(user_key) if len(user_key) > 50 else user_key
    idx_file = SESSION_LOG_DIR / f"{hashlib.md5((idx_key or user_key[:20]).encode()).hexdigest()[:12]}_sessions.json"
    data: dict[str, list] = {"sessions": []}
    if idx_file.exists():
        try:
            data = json.loads(idx_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    sessions = data.get("sessions", [])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    found = False
    for s in sessions:
        if s["id"] == sid:
            if name:
                s["name"] = name
            s["updated_at"] = now
            found = True
            break
    if not found:
        sessions.insert(0, {
            "id": sid,
            "name": name or f"New Session",
            "created_at": now,
            "updated_at": now,
        })
    data["sessions"] = sessions[:50]  # keep max 50
    idx_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _auto_name_session(public_key: str, request: Request, history: list, user_message: str) -> None:
    """Auto-name the session from the first user message (non-attachment text)."""
    sid = request.headers.get("X-Session-Id", "")
    if not sid:
        return
    user_count = sum(1 for m in history if m.get("role") == "user")
    if user_count != 2:
        return
    clean = user_message.replace("[GOVERNOR_IDENTITY:", "").split("[File attachment:")[0].strip()
    name = clean[:50].replace("\n", " ") if clean else ""
    if name:
        _save_session_index(public_key, sid, name)


def _pending_file(public_key: str) -> Path:
    import hashlib
    h = hashlib.md5(public_key.encode()).hexdigest()[:12]
    return SESSION_LOG_DIR / f"pending_{h}.json"


def _load_pending(public_key: str) -> list[dict]:
    pf = _pending_file(public_key)
    # 1. Try local file first
    if pf.exists():
        try:
            return json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 2. Fallback: load from GitHub (survives server restarts)
    try:
        import hashlib
        h = hashlib.md5(public_key.encode()).hexdigest()[:12]
        gh = GitHubClient()
        result = gh.read_file(_TRANSCRIPT_REPO, f"pending/{h}.json")
        if result.get("type") == "file":
            return json.loads(result["content"])
    except Exception:
        pass
    return []


def _save_pending(public_key: str, items: list[dict]) -> None:
    pf = _pending_file(public_key)
    pf.write_text(json.dumps(items, indent=2), encoding="utf-8")
    # Mirror to GitHub for durability
    asyncio.create_task(_sync_pending_to_github(public_key, items))


def _add_pending(public_key: str, proposal: dict) -> None:
    items = _load_pending(public_key)
    key = proposal.get("qr_code", "") or proposal.get("title", "")
    if key and not any(p.get("qr_code") == key or p.get("title") == key for p in items):
        items.append({
            "title": proposal.get("title", "Transaction"),
            "qr_code": proposal.get("qr_code", ""),
            "summary": proposal.get("summary", ""),
            "action": proposal.get("action", "submit_contribution"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        _save_pending(public_key, items)


def _resolve_pending(public_key: str, key: str, resolution: str) -> None:
    items = _load_pending(public_key)
    items = [p for p in items if p.get("qr_code") != key and p.get("title") != key]
    _save_pending(public_key, items)


_MERGE_PR_TITLE_RE = re.compile(r"^Merge PR #(\d+) on (\S+)")


def _cleanup_resolved_pending(public_key: str, gh: "GitHubClient | None" = None) -> int:
    """Drop pending merge_pr entries whose PR is no longer open.

    Fixes a stale-entry class of bug: when a governor merges a PR directly
    via GitHub instead of clicking Approve in the chat UI, the pending
    entry is never resolved server-side and keeps showing in the chat
    'Pending Approval' panel forever. This sweeps each `merge_pr` entry,
    looks up the live PR state via the GitHub API, and drops entries whose
    PR is `closed` or `merged`. Entries we can't verify (parse fail, API
    error) are kept conservatively.

    Returns the number of entries removed."""
    items = _load_pending(public_key)
    if not items:
        return 0
    if gh is None:
        try:
            gh = GitHubClient()
        except Exception:
            return 0
    cleaned: list[dict] = []
    removed = 0
    for item in items:
        if item.get("action") != "merge_pr":
            cleaned.append(item)
            continue
        m = _MERGE_PR_TITLE_RE.match(item.get("title", ""))
        if not m:
            cleaned.append(item)
            continue
        pr_num, repo = int(m.group(1)), m.group(2)
        try:
            pr = gh.g.get_repo(gh._full_name(repo)).get_pull(pr_num)
            if pr.state == "open":
                cleaned.append(item)
            else:
                removed += 1
                logger.info("Pending cleanup: dropped '%s' (PR is %s)", item.get("title"), pr.state)
        except Exception as e:
            # Conservative: keep entries we can't verify (typos, network blips, etc.)
            logger.debug("Pending cleanup: skipped '%s' (verify failed: %s)", item.get("title"), e)
            cleaned.append(item)
    if removed:
        _save_pending(public_key, cleaned)
    return removed


async def _pending_janitor_loop():
    """Periodic sweep of every pending_*.json under SESSION_LOG_DIR. Runs
    once shortly after startup, then every 12h. Skipped under DRY_RUN.

    Catches stale entries even for governors who don't open the chat UI
    (the lazy /pending GET path only cleans the active session's file)."""
    if settings.dry_run:
        logger.info("Pending janitor: DRY_RUN=true — skipping")
        return
    await asyncio.sleep(120)  # let other startup tasks finish
    while True:
        try:
            try:
                gh = GitHubClient()
            except Exception as e:
                logger.warning("Pending janitor: GitHubClient init failed (%s); sleeping", e)
                await asyncio.sleep(12 * 60 * 60)
                continue
            total_removed = 0
            for pf in SESSION_LOG_DIR.glob("pending_*.json"):
                try:
                    items = json.loads(pf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                # Reverse-derive the public_key — we don't have it; the
                # cleanup function takes it for the save path. Read+rewrite
                # by file directly.
                cleaned: list[dict] = []
                removed = 0
                for item in items:
                    if item.get("action") != "merge_pr":
                        cleaned.append(item)
                        continue
                    m = _MERGE_PR_TITLE_RE.match(item.get("title", ""))
                    if not m:
                        cleaned.append(item)
                        continue
                    pr_num, repo = int(m.group(1)), m.group(2)
                    try:
                        pr = gh.g.get_repo(gh._full_name(repo)).get_pull(pr_num)
                        if pr.state == "open":
                            cleaned.append(item)
                        else:
                            removed += 1
                            logger.info("Pending janitor: dropped '%s' from %s (PR is %s)", item.get("title"), pf.name, pr.state)
                    except Exception:
                        cleaned.append(item)
                if removed:
                    pf.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
                    total_removed += removed
            if total_removed:
                logger.info("Pending janitor: pruned %d total stale pending entries across all sessions", total_removed)
        except Exception as e:
            logger.warning("Pending janitor pass failed: %s", e)
        await asyncio.sleep(12 * 60 * 60)


async def _sync_pending_to_github(public_key: str, items: list[dict]) -> None:
    """Mirror pending approvals to GitHub for cross-server durability."""
    try:
        import hashlib
        h = hashlib.md5(public_key.encode()).hexdigest()[:12]
        gh = GitHubClient()
        content = json.dumps(items, indent=2)
        gh.commit_file(_TRANSCRIPT_REPO, "main", f"pending/{h}.json", content,
                       f"[autopilot] Update pending approvals ({len(items)} items)")
        logger.debug("Pending synced to GitHub: %d items", len(items))
    except Exception as e:
        logger.debug("Pending GitHub sync skipped: %s", e)


_TRANSCRIPT_REPO = "truesight_autopilot_transcript"


_REDACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:AWS_ACCESS_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:LLM_API_KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36,}\b"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{20,}"), "[REDACTED:SLACK_TOKEN]"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY[ -]*-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY[ -]*-----"), "[REDACTED:PRIVATE_KEY_BLOCK]"),
    # JWT-shaped tokens: 3 base64url segments separated by dots, each at least 8 chars
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:JWT]"),
    # env-style line: KEY_LIKE=secret-shaped-value (long, no spaces, base64-ish)
    (re.compile(r"^([A-Z][A-Z0-9_]{3,})=([A-Za-z0-9+/=_\-]{20,})$", re.MULTILINE), r"\1=[REDACTED:ENV_VALUE]"),
]


def _redact_secrets(text: str) -> tuple[str, list[str]]:
    """Redact common secret-shaped strings before public transcript publish.
    Returns (redacted_text, list_of_categories_that_matched)."""
    if not text:
        return text, []
    matched: list[str] = []
    for pattern, replacement in _REDACTION_PATTERNS:
        if pattern.search(text):
            tag = replacement.split(":", 1)[1].rstrip("]") if ":" in replacement else replacement
            matched.append(tag)
            text = pattern.sub(replacement, text)
    return text, matched


async def _publish_transcript(session_id: str, history: list[dict], governor_name: str | None = None, do_not_publish: bool = False) -> None:
    """Publish conversation transcript + uploaded images to public GitHub repo
    for DAO transparency. Runs as background task, never blocks the response.

    `do_not_publish=True` skips the publish entirely (set by caller via the
    chat payload's `do_not_publish` field — useful when chat content is
    sensitive and the governor doesn't want it on the public transcript repo)."""
    if do_not_publish:
        logger.info("Transcript publish skipped: do_not_publish=True for session %s", session_id[:16])
        return
    try:
        import hashlib as _hlib
        sid_hash = _hlib.md5(session_id.encode()).hexdigest()[:12]
        today = time.strftime("%Y-%m-%d", time.gmtime())

        gh = GitHubClient()

        # Build markdown transcript
        lines = [f"# Autopilot Session — {today}\n"]
        lines.append(f"**Session**: `{sid_hash}`\n")
        gov_name = governor_name
        if gov_name:
            lines.append(f"**Governor**: {gov_name}\n")
        lines.append("\n---\n\n")

        all_redactions: set[str] = set()
        for msg in history:
            role = msg.get("role", "")
            content = (msg.get("content", "") or "").strip()
            if not content:
                continue
            if role == "user":
                # Strip internal markers for public transcript
                content = content.replace("[GOVERNOR_IDENTITY:", "").split("[GOVERNOR_IDENTITY:")[-1]
                content, hits = _redact_secrets(content)
                all_redactions.update(hits)
                lines.append(f"### 🧑 Governor\n\n{content}\n\n")
            elif role == "assistant":
                # Strip XML tool-call syntax
                content = re.sub(r'<function_calls>.*?</function_calls>', '', content, flags=re.DOTALL).strip()
                if content:
                    content, hits = _redact_secrets(content)
                    all_redactions.update(hits)
                    lines.append(f"### 🤖 Autopilot\n\n{content}\n\n")
        if all_redactions:
            logger.info("Transcript redacted %d categories before publish: %s", len(all_redactions), sorted(all_redactions))

        transcript = "\n".join(lines)
        path = f"sessions/{today}/{sid_hash}/transcript.md"
        gh.commit_file(_TRANSCRIPT_REPO, "main", path, transcript,
                       f"[autopilot] Session {sid_hash} — {today}")

        # Publish uploaded images referenced in the conversation
        for msg in history:
            content = str(msg.get("content", ""))
            for match in re.finditer(r"\[IMG:(/uploads/[^\|]+)\|", content):
                img_src = UPLOAD_DIR / Path(match.group(1)).name
                if img_src.exists() and img_src.stat().st_size < 1_000_000:
                    img_bytes = img_src.read_bytes()
                    img_path = f"sessions/{today}/{sid_hash}/images/{img_src.name}"
                    try:
                        gh.commit_file(_TRANSCRIPT_REPO, "main", img_path,
                                      base64.b64encode(img_bytes).decode(),
                                      f"[autopilot] Image for session {sid_hash}")
                    except Exception:
                        pass  # images are best-effort

        logger.info("Published transcript to %s/%s", _TRANSCRIPT_REPO, path)
    except Exception as e:
        logger.debug("Transcript publish skipped: %s", e)


# ───────────────────────────── Message Queue ─────────────────────────────

@app.post("/chat/queue")
async def queue_message(request: Request) -> JSONResponse:
    """Queue a message for async processing. Returns position in queue."""
    body = await request.json()
    payload = body.get("payload")
    signature = body.get("signature")
    public_key = request.headers.get("X-Public-Key", "")

    if payload and signature and public_key:
        verify_payload(payload, signature, public_key)
        user_message = payload.get("message", "")
    else:
        public_key = verify_jwt(request)
        user_message = body.get("message", "")
        if not user_message:
            raise HTTPException(status_code=400, detail="message is required.")

    session_id = _session_key(public_key, request)
    msg_id = str(uuid.uuid4())
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    queued_msg = {
        "id": msg_id,
        "content": user_message,
        "timestamp": timestamp,
    }

    if session_id not in _message_queues:
        _message_queues[session_id] = []
    _message_queues[session_id].append(queued_msg)
    position = len(_message_queues[session_id])

    return JSONResponse({"queued": True, "position": position, "msg_id": msg_id})


@app.get("/chat/queue")
async def get_queue(request: Request) -> JSONResponse:
    """Return the current message queue for this session."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    session_id = _session_key(public_key, request)
    queue = _message_queues.get(session_id, [])
    return JSONResponse({"queue": queue})


@app.delete("/chat/queue/{msg_id}")
async def delete_queued_message(msg_id: str, request: Request) -> JSONResponse:
    """Remove a specific message from the queue by ID."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    session_id = _session_key(public_key, request)
    queue = _message_queues.get(session_id, [])
    for i, msg in enumerate(queue):
        if msg["id"] == msg_id:
            queue.pop(i)
            return JSONResponse({"status": "removed"})
    raise HTTPException(status_code=404, detail="Message not found in queue")


@app.patch("/chat/queue/{msg_id}")
async def update_queued_message(msg_id: str, request: Request) -> JSONResponse:
    """Update a queued message's content."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    body = await request.json()
    new_content = body.get("content", "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="content is required")
    session_id = _session_key(public_key, request)
    queue = _message_queues.get(session_id, [])
    for msg in queue:
        if msg["id"] == msg_id:
            msg["content"] = new_content
            msg["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return JSONResponse({"status": "updated"})
    raise HTTPException(status_code=404, detail="Message not found in queue")


@app.delete("/chat/active/{session_short}")
async def cancel_active_chat(session_short: str, request: Request) -> JSONResponse:
    """Cancel an in-flight /chat stream for the calling governor.

    `session_short` is the first 16 chars of the session_id (matches the
    short id used in logs/UI). The actual session_id is reconstructed from
    the caller's RSA public key + headers; the short id only confirms the
    caller meant to cancel the right session (anti-typo guard).

    Sets a cancel flag that the streaming loop checks at each round
    boundary AND inside `_heartbeat_until_done`, so an in-flight LLM call
    or tool call is cancelled within ~15s (the heartbeat interval)."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
    session_id = _session_key(public_key, request)
    if session_id[:16] != session_short:
        raise HTTPException(status_code=400, detail=f"session_short does not match this caller's session ({session_id[:16]}...)")
    if session_id not in _active_streams:
        return JSONResponse({"status": "no_active_stream", "session_id_short": session_short}, status_code=404)
    _cancel_flags[session_id] = True
    logger.info("Cancel requested for session %s", session_id[:16])
    return JSONResponse({"status": "cancel_requested", "session_id_short": session_short})


@app.post("/chat")
async def chat(request: Request):
    """SSE-streaming chat endpoint."""
    body = await request.json()
    payload = body.get("payload")
    signature = body.get("signature")
    public_key = request.headers.get("X-Public-Key", "")

    if payload and signature and public_key:
        verify_payload(payload, signature, public_key)
        user_message = payload.get("message", "")
        do_not_publish = bool(payload.get("do_not_publish", False))
    else:
        public_key = verify_jwt(request)
        user_message = body.get("message", "")
        do_not_publish = bool(body.get("do_not_publish", False))
        if not user_message:
            raise HTTPException(status_code=400, detail="message is required.")

    session_id = _session_key(public_key, request)
    history = _load_or_create_session(session_id)
    role = find_role_in_history(history)

    # Role detection for new topics
    if role is None:
        if len(history) == 0:
            # Brand new topic — if AUTOPILOT_DEFAULT_ROLE is configured,
            # silently boot into that role instead of prompting the operator.
            # Default ("general") is set in roles.get_default_role so the
            # out-of-box behavior is no prompt + all tools available.
            default = get_default_role()
            if default is not None:
                set_role_in_history(history, default)
                _log_session(session_id, history)
                # Fall through to the normal chat path — the user's actual
                # message gets processed under the default role.
                role = default
            else:
                # No default configured → preserve the original prompt behavior.
                history = build_role_menu()
                _log_session(session_id, history)
                return StreamingResponse(
                    _sse_single_response(ROLE_SELECTION_MESSAGE),
                    media_type="text/event-stream",
                )

    # Check for pending role (user is in "keep or reset?" flow)
    if role is None:
        pending = find_pending_role(history)
        if pending:
            clean = user_message.strip().lower()
            if clean in ("reset", "r", "yes", "y"):
                history = archive_old_history(history, pending)
                set_role_in_history(history, pending)
                _log_session(session_id, history)
                return StreamingResponse(
                    _sse_single_response(f"✅ Context reset. Role: **{pending.name}**.\n\nWhat would you like me to work on?"),
                    media_type="text/event-stream",
                )
            # "keep" or anything else — keep history, set role
            # Remove pending tag, set real role
            history = [m for m in history if not (isinstance(m.get("content", ""), str) and str(m["content"]).startswith("[PENDING_ROLE:"))]
            set_role_in_history(history, pending)
            _log_session(session_id, history)
            return StreamingResponse(
                _sse_single_response(f"✅ Keeping existing context. Role: **{pending.name}**.\n\nWhat would you like me to work on?"),
                media_type="text/event-stream",
            )

        # Session exists but no role set — try to parse user message as role choice
        role = resolve_role(user_message)
        if role:
            msg_count = sum(1 for m in history if m.get("role") in ("user", "assistant"))
            if msg_count >= RESET_CONTEXT_THRESHOLD:
                # Large existing session — ask about reset before committing
                history.insert(0, {"role": "system", "content": pending_role_tag(role)})
                _log_session(session_id, history)
                return StreamingResponse(
                    _sse_single_response(reset_context_prompt(role, msg_count)),
                    media_type="text/event-stream",
                )
            set_role_in_history(history, role)
            _log_session(session_id, history)
            return StreamingResponse(
                _sse_single_response(f"✅ Role set: **{role.name}**.\n\nWhat would you like me to work on?"),
                media_type="text/event-stream",
            )
        # Still no role match — show menu again
        return StreamingResponse(
            _sse_single_response(f"🤔 I couldn't parse a role from that. Please pick a number (1–7) or role name:\n\n{ROLE_SELECTION_MESSAGE}"),
            media_type="text/event-stream",
        )

    # Inject governor identity so the LLM knows who "I" / "me" refers to
    gov_name = _gov_name_for_key(public_key)
    if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
        user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"

    history.append({"role": "user", "content": user_message})
    _auto_name_session(public_key, request, history, user_message)
    _log_session(session_id, history)

    # Track session as active — survives client disconnect
    _active_streams[session_id] = time.time()
    # Clear any stale cancel flag from a prior turn so this fresh request runs
    _cancel_flags.pop(session_id, None)

    async def _stream_with_cleanup():
        # Hold the per-session lock for the whole streamed turn so a second
        # same-thread request can't run its turn concurrently (one writer / one
        # executor per thread). Different sessions have different locks → parallel.
        async with _session_lock(session_id):
            try:
                async for event in _stream_chat(user_message, history, session_id, governor_name=gov_name, do_not_publish=do_not_publish, role=role):
                    yield event
            finally:
                _active_streams.pop(session_id, None)
                _cancel_flags.pop(session_id, None)

    return StreamingResponse(
        _stream_with_cleanup(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat/upload")
async def chat_upload(
    request: Request,
    files: List[UploadFile] = File(...),
):
    """Accepts a chat message + one or more file attachments. Processes all
    images with pyzbar + Grok, then hands combined analysis to DeepSeek."""
    public_key = request.headers.get("X-Public-Key", "")
    payload_raw = request.headers.get("X-Payload", "")
    signature = request.headers.get("X-Signature", "")

    if payload_raw and signature and public_key:
        payload = json.loads(payload_raw)
        verify_payload(payload, signature, public_key)
        user_message_text = payload.get("message", "")
    else:
        public_key = verify_jwt(request)
        user_message_text = ""

    session_id = _session_key(public_key, request)
    attachment_info = None

    # Process all uploaded files
    processed_files: list[dict] = []
    for upload_file in files:
        if not upload_file.filename:
            continue
        if not upload_file.filename:
            continue
        ext = Path(upload_file.filename).suffix or ""
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / safe_name
        content = await upload_file.read()
        dest.write_bytes(content)

        mime_type = upload_file.content_type or mimetypes.guess_type(upload_file.filename)[0] or "application/octet-stream"
        size_kb = round(len(content) / 1024, 1)
        converted = False

        if ext.lower() in (".heic", ".heif") and len(content) < 10 * 1024 * 1024:
            try:
                jpg_dest = UPLOAD_DIR / f"{dest.stem}.jpg"
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(dest), "--out", str(jpg_dest)],
                    capture_output=True, timeout=30, check=True,
                )
                jpg_content = jpg_dest.read_bytes()
                mime_type = "image/jpeg"
                size_kb = round(len(jpg_content) / 1024, 1)
                content = jpg_content
                dest = jpg_dest
                safe_name = jpg_dest.name
                converted = True
                logger.info("Converted HEIC %s to JPEG (%d KB)", upload_file.filename, size_kb)
            except Exception as e:
                logger.warning("HEIC conversion failed for %s: %s", upload_file.filename, e)

        processed_files.append({
            "filename": upload_file.filename,
            "dest": str(dest),
            "mime_type": mime_type,
            "size_kb": size_kb,
            "converted": converted,
            "img_url": f"/uploads/{safe_name}",
        })

    if not processed_files:
        raise HTTPException(status_code=400, detail="No valid files to process.")

    # Build a streaming generator that processes all files
    async def _upload_chat_stream():
        all_pyzbar: list[dict] = []
        all_grok: dict | None = None
        total = len(processed_files)

        yield _sse_event("status", {"stage": "upload", "message": f"Processing {total} file{'s' if total > 1 else ''}..."})

        # Pyzbar scan all files
        yield _sse_event("status", {"stage": "pyzbar", "message": f"Scanning {total} image{'s' if total > 1 else ''} for barcodes..."})
        jpg_paths: list[str] = []
        for pf in processed_files:
            if pf["mime_type"].startswith("image/"):
                try:
                    result = scan_qr_from_file(pf["dest"])
                    all_pyzbar.append({"file": pf["filename"], "result": result})
                except Exception:
                    pass
            jpg_paths.append(pf["dest"])

        # Grok vision — send all images in one batch call
        if jpg_paths:
            yield _sse_event("status", {"stage": "grok", "message": f"Analyzing {len(jpg_paths)} image{'s' if len(jpg_paths) > 1 else ''} with Grok..."})
            try:
                all_grok = await asyncio.wait_for(
                    asyncio.to_thread(grok_analyze_images, jpg_paths, "", GROK_MODEL, 0.2, 60.0),
                    timeout=70.0,
                )
            except Exception:
                pass

        yield _sse_event("status", {"stage": "done", "message": "Analysis complete, preparing response..."})

        # Build combined content part
        content_parts = []
        for pf in processed_files:
            cp = (
                f"[IMG:{pf['img_url']}|{pf['filename']}|{pf['mime_type']}]\n"
                f"[File: {pf['filename']} ({pf['mime_type']}, {pf['size_kb']} KB)]\n"
            )
            if pf["converted"]:
                cp += "(Converted from HEIC to JPEG)\n"
            content_parts.append(cp)

        # Pyzbar results
        any_pyzbar = [p for p in all_pyzbar if p["result"].get("status") == "success" and p["result"].get("codes")]
        if any_pyzbar:
            cp = "\n=== PYZBAR SCAN RESULTS ===\n"
            for p in any_pyzbar:
                codes = p["result"]["codes"]
                cp += f"\n{p['file']}: {len(codes)} code(s)\n"
                for c in codes:
                    cp += f"  - {c['type']}: {c['data']}\n"
            content_parts.append(cp)

        # Grok results
        if all_grok and all_grok.get("status") == "success":
            cp = "\n=== GROK VISION ANALYSIS ===\n"
            if desc := all_grok.get("image_description"):
                cp += f"Scene: {desc}\n"
            if guess := all_grok.get("product_type_guess"):
                cp += f"Product: {guess}\n"
            if labels := all_grok.get("label_text_visible"):
                cp += f"Label text: {'; '.join(labels)}\n"
            if quality := all_grok.get("photo_quality"):
                cp += f"Photo quality: {quality}\n"
            if qr_guesses := all_grok.get("qr_codes_guessed"):
                for g in qr_guesses:
                    conf_pct = int(g.get('confidence', 0) * 100)
                    cp += f"Grok GUESSED QR: {g['data']} (confidence: {conf_pct}%)\n"
            if bc_guesses := all_grok.get("barcodes_guessed"):
                for g in bc_guesses:
                    conf_pct = int(g.get('confidence', 0) * 100)
                    cp += f"Grok GUESSED barcode: {g.get('type','?')}: {g['data']} (confidence: {conf_pct}%)\n"
            if notes := all_grok.get("notes"):
                cp += f"Notes: {notes}\n"
            content_parts.append(cp)

        content_parts.append(
            "\n## INSTRUCTIONS\n"
            "For EACH Agroverse QR code found above, output a batch approval JSON array in this format:\n"
            "```json\n"
            "[{\"action\": \"submit_contribution\", \"title\": \"Move QR 2024OSCAR_...\", \"qr_code\": \"2024OSCAR_...\", \"summary\": \"Ceremonial Cacao Kraft Pouch from Kirsten Ritschel to Gary Teh\"}]\n"
            "```\n"
            "Include ALL found QR codes. The user will click Accept on each one individually."
        )

        content_part = "\n".join(content_parts)
        filenames = ", ".join(pf["filename"] for pf in processed_files)
        user_message = f"{user_message_text}\n\nAttached: {filenames}\n{content_part}" if user_message_text.strip() else f"Attached: {filenames}\n{content_part}"

        history = _load_or_create_session(session_id)
        role = find_role_in_history(history)
        # If no role, default to general (upload endpoints always have history from scanning step)
        gov_name = _gov_name_for_key(public_key)
        if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
            user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"
        history.append({"role": "user", "content": user_message})
        _auto_name_session(public_key, request, history, user_message)
        _log_session(session_id, history)

        async for event in _stream_chat(user_message, history, session_id, attachment_info=attachment_info, governor_name=gov_name, role=role):
            yield event

    return StreamingResponse(
        _upload_chat_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ──────────────────── Non-streaming fallback chat ────────────────────

@app.post("/chat-blocking")
async def chat_blocking(request: Request) -> JSONResponse:
    """Non-streaming fallback for clients that don't support SSE."""
    body = await request.json()
    payload = body.get("payload")
    signature = body.get("signature")
    public_key = request.headers.get("X-Public-Key", "")

    if payload and signature and public_key:
        verify_payload(payload, signature, public_key)
        user_message = payload.get("message", "")
    else:
        public_key = verify_jwt(request)
        user_message = body.get("message", "")
        if not user_message:
            raise HTTPException(status_code=400, detail="message is required.")

    session_id = _session_key(public_key, request)
    # Serialize per session (tg:<chat>:<thread>): one writer / one executor per
    # thread. A second same-thread request waits for the in-flight turn instead of
    # interleaving its writes — the race that bricked threads 3 and 780. Different
    # threads have different locks, so they still run concurrently.
    async with _session_lock(session_id):
        return await _chat_blocking_turn(session_id, user_message, public_key)


async def _chat_blocking_turn(session_id: str, user_message: str, public_key: str) -> JSONResponse:
    history = _load_or_create_session(session_id)
    role = find_role_in_history(history)

    # Role detection for new topics
    if role is None:
        if len(history) == 0:
            # If AUTOPILOT_DEFAULT_ROLE is configured, silently boot into
            # that role instead of prompting. Default is "general".
            default = get_default_role()
            if default is not None:
                set_role_in_history(history, default)
                _log_session(session_id, history)
                role = default
            else:
                history = build_role_menu()
                _log_session(session_id, history)
                return JSONResponse({"response": ROLE_SELECTION_MESSAGE})

    if role is None:
        # Check for pending role (user is in "keep or reset?" flow)
        pending = find_pending_role(history)
        if pending:
            clean = user_message.strip().lower()
            if clean in ("reset", "r", "yes", "y"):
                history = archive_old_history(history, pending)
                set_role_in_history(history, pending)
                _log_session(session_id, history)
                return JSONResponse({"response": f"✅ Context reset. Role: **{pending.name}**.\n\nWhat would you like me to work on?"})
            history = [m for m in history if not (isinstance(m.get("content", ""), str) and str(m["content"]).startswith("[PENDING_ROLE:"))]
            set_role_in_history(history, pending)
            _log_session(session_id, history)
            return JSONResponse({"response": f"✅ Keeping existing context. Role: **{pending.name}**.\n\nWhat would you like me to work on?"})

        role = resolve_role(user_message)
        if role:
            msg_count = sum(1 for m in history if m.get("role") in ("user", "assistant"))
            if msg_count >= RESET_CONTEXT_THRESHOLD:
                history.insert(0, {"role": "system", "content": pending_role_tag(role)})
                _log_session(session_id, history)
                return JSONResponse({"response": reset_context_prompt(role, msg_count)})
            set_role_in_history(history, role)
            _log_session(session_id, history)
            return JSONResponse({"response": f"✅ Role set: {role.name}.\n\nWhat would you like me to work on?"})
        return JSONResponse({"response": f"🤔 I couldn't parse a role from that. Please pick a number (1–7) or role name:\n\n{ROLE_SELECTION_MESSAGE}"})

    gov_name = _gov_name_for_key(public_key)
    if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
        user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"
    history.append({"role": "user", "content": user_message})

    system_prompt = get_system_prompt_for_role(role)
    tools = get_tool_schemas_for_role(role)
    client = LLMClient()

    # Multi-round tool loop (the streaming path loops; this one used to run a
    # single round, which truncated multi-step answers and could leak an
    # unexecuted tool call as text). Keep running tool rounds until the model
    # returns a final text answer or we hit the round budget.
    max_rounds = int(os.getenv("CHAT_BLOCKING_MAX_ROUNDS", "15"))
    assistant_text = ""
    tool_trace: list[dict] = []
    try:
        for _round in range(max_rounds):
            completion = client.chat(system_prompt, history, tools=tools)
            message = completion["choices"][0].get("message", {})
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                assistant_text = client.extract_text(completion)
                break
            history.append({
                "role": "assistant",
                "content": message.get("content", ""),
                "tool_calls": [
                    {"id": tc["id"], "type": tc["type"],
                     "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments")
                try:
                    func_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, TypeError):
                    func_args = {}
                result_text = await _run_tool(func_name, func_args, history, session_id, gov_name)
                tool_trace.append({"name": func_name, "result": result_text})
                history.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})

        # Force a clean text-only answer if we exhausted the budget, came back
        # blank, or the model leaked a text-format tool call instead of executing it.
        if (not assistant_text or not assistant_text.strip()
                or "<tool_call>" in assistant_text
                or assistant_text in ("(empty response)", "(no response)")):
            logger.info("Forcing text-only completion (rounds exhausted / blank / leaked tool-call)")
            completion = client.chat(system_prompt, history, tools=None)
            assistant_text = client.extract_text(completion)

    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Never return a blank response — Telegram (and other clients) reject empty text.
    if not assistant_text or not assistant_text.strip():
        assistant_text = "(Autopilot produced an empty response — try rephrasing or breaking the request into smaller steps.)"

    proposal = None
    try:
        json_match = re.search(r"```json\s*(\{.*?\})\s*```", assistant_text, re.DOTALL)
        if json_match:
            embedded = json.loads(json_match.group(1))
            if "proposal" in embedded:
                proposal = embedded["proposal"]
                assistant_text = re.sub(r"```json\s*\{.*?\}\s*```", "", assistant_text, flags=re.DOTALL).strip()
    except Exception:
        pass

    # Per-turn actions-taken report (invariant 7) — same as the streaming path.
    assistant_text = _append_turn_report(assistant_text, {"tool_trace": tool_trace})

    history.append({"role": "assistant", "content": assistant_text})
    _sessions[session_id] = history

    response_data: dict[str, Any] = {"response": assistant_text}
    if proposal:
        response_data["proposal"] = proposal
    return JSONResponse(response_data)


@app.post("/refresh-context")
async def refresh_context(request: Request) -> JSONResponse:
    verify_jwt(request)
    new_prompt = refresh_system_prompt()
    return JSONResponse({"status": "refreshed", "prompt_length": len(new_prompt)})


@app.get("/governors")
async def list_governors(request: Request) -> JSONResponse:
    verify_jwt(request)
    data = load_governors()
    governors = data.get("governors", [])
    return JSONResponse({
        "count": len(governors),
        "updated_at": data.get("updated_at", ""),
        "source": data.get("source", ""),
        "governors": [
            {"name": g.get("name"), "email": g.get("email"), "status": g.get("status")}
            for g in governors
        ],
    })


@app.post("/governors/refresh")
async def force_refresh_governors(request: Request) -> JSONResponse:
    verify_jwt(request)
    data = refresh_governor_cache()
    return JSONResponse({
        "status": "refreshed",
        "count": len(data.get("governors", [])),
        "updated_at": data.get("updated_at", ""),
    })


@app.post("/admin/deploy")
async def admin_deploy(request: Request) -> JSONResponse:
    """Self-deploy: git pull + restart. Returns git result before restarting."""
    from .auth import verify_jwt
    verify_jwt(request)
    import subprocess, os
    repo_dir = "/opt/truesight_autopilot"
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True, text=True, timeout=30, cwd=repo_dir,
        )
        git_out = result.stdout + result.stderr
        # Fork restart so we can return before dying
        pid = os.fork()
        if pid == 0:
            import time
            time.sleep(1)
            subprocess.run(
                ["systemctl", "restart", "truesight-autopilot"],
                capture_output=True, timeout=10,
            )
            os._exit(0)
        return JSONResponse({
            "status": "restarting",
            "git_output": git_out,
            "message": "Service restarting in background. Check health after ~10s.",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ───────────────────────────── Autopilot ─────────────────────────────

# Track errors for self-healing
_self_heal_errors: list[dict] = []
_SELF_HEAL_THRESHOLD = 3  # consecutive errors before opening a fix PR
_SELF_HEAL_WINDOW = 3600  # seconds


def _looks_base64(s: str) -> bool:
    """Quick heuristic: base64 strings have no spaces and are mostly alphanumeric with +/=/."""
    return " " not in s[:100] and len(s) > 50 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n" for c in s[:200].replace("\n", ""))


_RECOVERED_TOOL_RESULT = "[tool result lost to a concurrent-write race; session auto-recovered]"


def _sanitise_tool_messages(history: list[dict]) -> None:
    """Make the message list valid for the OpenAI/DeepSeek tool protocol, in place.

    Two failure modes corrupt a transcript — almost always via a concurrent-write
    race (two turns for the same thread interleaving their writes):

    1. **Orphaned ``tool`` message** — a tool result with no preceding assistant
       ``tool_calls`` that owns its ``tool_call_id``. DeepSeek: "Messages with role
       tool must be a response to a preceding message with tool_calls."
    2. **Orphaned ``tool_calls``** — an assistant message whose ``tool_calls`` are
       NOT each followed by a ``tool`` result. DeepSeek: "An assistant message with
       'tool_calls' must be followed by tool messages responding to each
       'tool_call_id'."

    (1) is healed by dropping the orphan tool message. (2) is healed by injecting a
    synthetic ``tool`` result so the assistant turn stays well-formed and the thread
    is never bricked — worst case we lose one tool's output and say so. Healing both
    directions means a raced transcript degrades gracefully instead of 400-ing every
    subsequent reply in that thread forever.
    """
    # Pass 1 — drop orphaned tool messages (result with no owning tool_calls).
    known_call_ids: set = set()
    i = 0
    while i < len(history):
        m = history[i]
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []) or []:
                known_call_ids.add(tc.get("id", ""))
        if m.get("role") == "tool":
            if m.get("tool_call_id", "") not in known_call_ids:
                logger.info("Dropped orphaned tool message at index %d id=%s", i, m.get("tool_call_id", ""))
                history.pop(i)
                continue
        i += 1

    # Pass 2 — heal orphaned tool_calls (assistant tool_calls lacking results).
    i = 0
    while i < len(history):
        m = history[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id", "") for tc in m.get("tool_calls", [])]
            j = i + 1
            seen: set = set()
            while j < len(history) and history[j].get("role") == "tool":
                seen.add(history[j].get("tool_call_id", ""))
                j += 1
            missing = [cid for cid in ids if cid not in seen]
            if missing:
                stubs = [
                    {"role": "tool", "tool_call_id": cid, "content": _RECOVERED_TOOL_RESULT}
                    for cid in missing
                ]
                history[j:j] = stubs  # insert right after the existing contiguous tool run
                logger.info("Healed %d orphaned tool_call(s) at assistant index %d", len(stubs), i)
                i = j + len(stubs)
                continue
        i += 1


def _record_chat_error(error_detail: str) -> None:
    """Record a chat error for self-healing analysis."""
    now = time.time()
    _self_heal_errors.append({"time": now, "error": error_detail})
    # Prune old entries
    _self_heal_errors[:] = [e for e in _self_heal_errors if now - e["time"] < _SELF_HEAL_WINDOW]


async def _self_heal_loop():
    """Background loop: check for recent errors and open fix PRs."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            now = time.time()
            recent = [e for e in _self_heal_errors if now - e["time"] < _SELF_HEAL_WINDOW]
            if len(recent) >= _SELF_HEAL_THRESHOLD:
                logger.warning("Self-heal triggered: %d errors in window", len(recent))
                patterns = "\n".join(recent[-5:])
                fixer = FixAgent()
                pr_url = fixer.run_simple(
                    "truesight_autopilot",
                    f"Autopilot detected {len(recent)} chat errors:\n{patterns}\n\nDiagnose and fix the root cause.",
                )
                if pr_url:
                    logger.info("Self-heal PR opened: %s", pr_url)
                    _self_heal_errors.clear()
        except Exception as e:
            logger.error("Self-heal loop error: %s", e)


def _update_context_after_fix(repo: str, pr_url: str, summary: str) -> None:
    """Append a summary of significant changes to agentic_ai_context/CONTEXT_UPDATES.md."""
    try:
        gh = GitHubClient()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"- {now} — [{repo}]({pr_url}): {summary}\n"

        # Read current CONTEXT_UPDATES.md
        result = gh.read_file("agentic_ai_context", "CONTEXT_UPDATES.md")
        if result.get("type") == "file":
            content = result["content"]
        else:
            content = "# Context Updates\n\nAutopilot logs significant changes here so other AIs can stay up to date.\n\n"

        # Prepend new entry
        new_content = content.replace("# Context Updates\n\n", f"# Context Updates\n\n{entry}")

        # Commit to a branch and open PR
        branch = f"autopilot/context-update-{int(time.time())}"
        repo = gh.get_repo("TrueSightDAO", "agentic_ai_context")
        base = repo.get_branch("main")
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base.commit.sha)
        try:
            existing = repo.get_contents("CONTEXT_UPDATES.md", ref=branch)
            repo.update_file("CONTEXT_UPDATES.md", f"[autopilot] Context update: {repo} fix",
                             new_content, existing.sha, branch=branch)
        except Exception:
            repo.create_file("CONTEXT_UPDATES.md", f"[autopilot] Context update: {repo} fix",
                             new_content, branch=branch)
        pr = repo.create_pull(
            title=f"[autopilot] Context update: {repo} fix",
            body=f"## Context Update\n\n{summary}\n\nTriggered by: {pr_url}\n\nThis PR was automatically generated by truesight_autopilot.",
            head=branch, base="main",
        )
        logger.info("Context update PR opened: %s", pr.html_url)
    except Exception as e:
        logger.error("Failed to update context: %s", e)

_AUTOPILOT_BRANCH_PREFIX = "autopilot/fix-"


@app.post("/webhook/github")
async def github_webhook(payload: dict):
    """GitHub webhook handler. Currently handles `pull_request.closed` events
    to clean up the `autopilot/fix-*` branch the autopilot created for the PR.
    Other event types are logged and ignored."""
    action = payload.get("action", "unknown")
    pr = payload.get("pull_request") or {}
    head_ref = (pr.get("head") or {}).get("ref", "")
    repo_name = ((payload.get("repository") or {}).get("name") or "").strip()

    logger.info("GitHub webhook received: action=%s repo=%s head_ref=%s", action, repo_name, head_ref)

    if action == "closed" and head_ref.startswith(_AUTOPILOT_BRANCH_PREFIX) and repo_name in settings.allowed_repos:
        try:
            gh = GitHubClient()
            ok = gh.delete_branch(repo_name, head_ref)
            if ok:
                logger.info("Janitor: deleted closed-PR branch %s on %s", head_ref, repo_name)
                return {"status": "branch_deleted", "repo": repo_name, "branch": head_ref}
            return {"status": "branch_delete_failed", "repo": repo_name, "branch": head_ref}
        except Exception as e:
            logger.warning("Janitor webhook handler failed: %s", e)
            return {"status": "error", "message": str(e)}
    return {"status": "received", "action": action}


async def _context_sync_loop():
    """Periodic hard-refresh of the read-only context mirrors (agentic_ai_context,
    tokenomics) so handoff plans and docs committed since the last deploy are
    visible to read_context_file / search_context — without relying on an LLM to
    remember the manual pull-first rule. Refreshes shortly after startup, then
    every settings.context_sync_interval_seconds. Skipped under DRY_RUN."""
    if settings.dry_run:
        logger.info("Context sync: DRY_RUN=true — skipping context sync loop")
        return
    await asyncio.sleep(10)  # let startup settle, then make context fresh early
    while True:
        try:
            # Run the blocking git off the event loop so a multi-second fetch
            # never stalls Telegram message pickup / posting. The in-place reset
            # is mutually excluded from context reads via CONTEXT_REFRESH_LOCK
            # (app/context.py), so there is no torn-read race either.
            results = await asyncio.to_thread(refresh_context_repos)
            errs = {k: v for k, v in results.items() if v != "ok"}
            if errs:
                logger.warning("Context sync: results=%s", results)
            else:
                logger.info("Context sync: refreshed %s", results or "(no mirrors found)")
        except Exception as e:
            logger.warning("Context sync pass failed: %s", e)
        await asyncio.sleep(settings.context_sync_interval_seconds)


async def _branch_janitor_loop():
    """Periodic janitor that prunes orphan `autopilot/fix-*` branches >30 days
    old across all allowed repos. Runs once at startup (after a short delay)
    and then every 24h. Skipped under DRY_RUN."""
    if settings.dry_run:
        logger.info("Janitor: DRY_RUN=true — skipping branch janitor loop")
        return
    await asyncio.sleep(60)  # let the rest of startup finish
    while True:
        try:
            gh = GitHubClient()
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=30)
            total_deleted = 0
            for repo in settings.allowed_repos:
                try:
                    branches = gh.list_branches_matching(repo, _AUTOPILOT_BRANCH_PREFIX)
                except Exception as e:
                    logger.warning("Janitor: list_branches failed on %s: %s", repo, e)
                    continue
                for b in branches:
                    last_at = b.get("last_commit_at", "")
                    try:
                        ts = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if ts > cutoff:
                        continue
                    if gh.delete_branch(repo, b["name"]):
                        total_deleted += 1
            if total_deleted:
                logger.info("Janitor: pruned %d stale autopilot branches across allowed repos", total_deleted)
        except Exception as e:
            logger.warning("Janitor pass failed: %s", e)
        await asyncio.sleep(24 * 60 * 60)


@app.get("/tools/generate-ssh-key")
async def generate_ssh_key():
    """Generate an ed25519 SSH keypair for the autopilot.

    Creates ~/.ssh/id_ed25519_truesight_autopilot if it doesn't exist,
    configures ~/.ssh/config for github.com, and returns the public key.
    The autopilot calls this via http_fetch to provision its own SSH key.
    """
    import subprocess
    import os
    from pathlib import Path

    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key_path = ssh_dir / "id_ed25519_truesight_autopilot"

    if not key_path.exists():
        try:
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", f"truesight-autopilot-{os.uname().nodename}"],
                capture_output=True, timeout=30, check=True,
            )
            key_path.chmod(0o600)
            (ssh_dir / "id_ed25519_truesight_autopilot.pub").chmod(0o644)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # Configure ~/.ssh/config for github.com
    config_path = ssh_dir / "config"
    github_block = "\nHost github.com\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/id_ed25519_truesight_autopilot\n  IdentitiesOnly yes\n"
    if config_path.exists():
        existing = config_path.read_text()
        if "Host github.com" not in existing:
            config_path.write_text(existing + github_block)
    else:
        config_path.write_text(github_block)
    config_path.chmod(0o600)

    pub_key = (ssh_dir / "id_ed25519_truesight_autopilot.pub").read_text().strip()
    return JSONResponse({
        "status": "success",
        "public_key": pub_key,
        "key_path": str(key_path),
        "message": "SSH key is ready. Add this public key to GitHub (Settings > SSH keys) and to any EC2 instances (~/.ssh/authorized_keys) you want the autopilot to manage.",
    })


@app.get("/metrics")
async def metrics():
    return JSONResponse(content={"prs_opened_today": 0, "emails_processed": 0})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Add CORS headers so oracle.truesight.me can see error responses
    if request.url.path == "/oracle-advisory":
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=_CORS_HEADERS)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
