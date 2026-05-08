"""FastAPI application for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, List

import mimetypes
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

from .auth import create_jwt, verify_jwt, verify_payload
from .config import settings
from .context import get_system_prompt, refresh_system_prompt, get_context_file
from .governor_registry import refresh_cache as refresh_governor_cache, load_governors


def _gov_name_for_key(public_key_b64: str) -> str | None:
    """Look up governor name from public key. Returns name or None."""
    data = load_governors()
    for g in data.get("governors", []):
        if g.get("public_key") == public_key_b64:
            return g.get("name")
    return None
from .llm_client import LLMClient, LLMError, get_tool_schemas
from .tools.github_tools import read_repo_file
from .tools.qr_scanner import scan_qr_from_file, scan_qr_batch, lookup_qr_code, lookup_qr_batch
from .tools.dao_identity import register_identity
from .tools.inventory_lookup import list_matching_qr_codes
from .tools.fs_tools import list_directory, read_local_file
from .grok_client import grok_analyze_images, GROK_MODEL
from .fix_agent import FixAgent
from .github_client import GitHubClient
from .email_poller import EmailPoller
from .aws_monitor import AWSMonitor
from .edgar_logger import EdgarLogger as EdgarDirectClient

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger("autopilot")

email_poller: EmailPoller | None = None
aws_monitor: AWSMonitor | None = None
_sessions: dict[str, list[dict[str, str]]] = {}
_pending_submissions: dict[str, dict] = {}  # session_key -> proposed submission awaiting approval
_active_streams: dict[str, float] = {}  # session_key -> last activity timestamp
_message_queues: dict[str, list[dict]] = {}  # session_key -> list of queued messages
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
            # Clean legacy XML leaks from old sessions
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str) and "<function_calls>" in content:
                    m["content"] = re.sub(r'<function_calls>.*?</function_calls>', '', content, flags=re.DOTALL).strip()
            _sessions[session_key] = messages
            logger.info("Restored session %s with %d messages", sid_hash, len(messages))
            return messages
        except Exception:
            pass

    _sessions[session_key] = []
    return _sessions[session_key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global email_poller, aws_monitor
    logger.info("Autopilot starting up...")

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
    else:
        logger.info("DRY_RUN=true — no background tasks started")

    yield

    logger.info("Autopilot shutting down...")


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

    return JSONResponse({"messages": visible, "session_id": session_id})


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
    """Return pending approvals for this governor (persistent, survives refresh)."""
    public_key = request.headers.get("X-Public-Key", "")
    if not public_key:
        raise HTTPException(status_code=400, detail="X-Public-Key header required")
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
        repo_name = func_args.get("repo", "")
        issue = func_args.get("issue_description", "")
        allowed = settings.allowed_repos
        if repo_name not in allowed:
            return f"Error: repo '{repo_name}' not in allowed list."
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
    if func_name == "create_dao_submission":
        title = func_args.get("title", "")
        body = func_args.get("body", "")
        pr_urls = func_args.get("pr_urls", [])
        contributors = func_args.get("contributors", governor_name or "autopilot@agroverse.shop")
        amount = func_args.get("amount", "0")
        tdg_issued = func_args.get("tdg_issued", "0")
        if not title or not body or not pr_urls:
            return json.dumps({"status": "error", "message": "title, body, and pr_urls are required"})
        edgar = EdgarDirectClient()
        if not edgar.is_configured():
            return json.dumps({"status": "error", "message": "Edgar credentials not configured — cannot submit"})
        pr_block = "Pull requests (GitHub evidence):\n" + "\n".join(f"- {u.strip()}" for u in pr_urls)
        description = f"{title}\n\n{pr_block}\n\nDetails:\n{body}"
        attrs = {
            "Type": "Time (Minutes)" if amount == "0" or float(amount) > 60 else "USD",
            "Amount": amount,
            "Description": description,
            "Contributor(s)": contributors,
            "TDG Issued": tdg_issued,
            "Attached Filename": "N/A",
            "Destination Contribution File Location": "N/A",
        }
        ok = edgar.submit_contribution("CONTRIBUTION EVENT", attrs, description=title)
        return json.dumps({"status": "success" if ok else "error", "message": "Contribution submitted" if ok else "Submission failed"})
    if func_name == "upload_file_to_github":
        from .tools.upload_file_to_github import upload_file_to_github as _upload
        result = _upload(
            repo=func_args.get("repo", ""),
            path=func_args.get("path", ""),
            content=func_args.get("content", ""),
            message=func_args.get("message", "Upload via autopilot"),
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
    return f"Unknown tool: {func_name}"


async def _stream_chat(user_message: str, history: list[dict], session_id: str, attachment_info: dict | None = None, governor_name: str | None = None):
    system_prompt = get_system_prompt()
    client = LLMClient()
    tools = get_tool_schemas()
    req_id = int(time.time() * 1000) % 1000000
    logger.info("[%d] CHAT REQ: session=%s msg=%.150s attach=%s", req_id, session_id[:16], user_message, attachment_info is not None)

    # Trim history to avoid context overflow (max ~400K chars = ~100K tokens for messages)
    # deepseek-chat has 128K context; leave ~28K for system prompt, tools, and completion
    MAX_HISTORY_CHARS = 400_000
    total = sum(len(msg.get("content", "")) for msg in history)
    while total > MAX_HISTORY_CHARS and len(history) > 4:
        removed = history.pop(0)
        total -= len(removed.get("content", ""))


    try:
        MAX_TOOL_ROUNDS = int(os.getenv("CHAT_MAX_TOOL_ROUNDS", "15"))
        assistant_text = ""
        round_num = 0

        while round_num < MAX_TOOL_ROUNDS:
            round_num += 1
            completion = client.chat(system_prompt, history, tools=tools)
            _log_raw_llm(session_id, f"deepseek-round-{round_num}", completion["choices"][0] if "choices" in completion else completion)
            assistant_message = completion["choices"][0].get("message", {})
            tool_calls = client.extract_tool_calls(completion)
            logger.info("[%d] DEEPSEEK RESP round=%d tools=%d tokens=%s", req_id, round_num,
                         len(tool_calls),
                         completion.get("usage", {}).get("total_tokens", "?"))

            tool_calls = assistant_message.get("tool_calls", [])

            if tool_calls:
                # Stream initial thought
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
                    func_args = json.loads(tc["function"]["arguments"])
                    tool_call_id = tc["id"]
                    logger.info("[%d] TOOL CALL: %s args=%.200s", req_id, func_name, json.dumps(func_args))

                    yield _sse_event("tool", {"tool": func_name, "status": "calling"})
                    result_text = await _run_tool(func_name, func_args, history, session_id, governor_name)
                    yield _sse_event("tool", {"tool": func_name, "status": "done"})
                    logger.info("[%d] TOOL RESULT: %s result=%.300s", req_id, func_name, result_text[:300])

                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result_text,
                    })
            else:
                assistant_text = client.extract_text(completion)
                break  # no more tool calls — exit loop

        # If still empty after all rounds (LLM only returned tool_calls), force text-only
        if not assistant_text:
            logger.info("[%d] Empty after %d tool rounds — forcing text-only completion", req_id, round_num)
            completion = client.chat(system_prompt, history, tools=None)
            assistant_text = client.extract_text(completion)

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
    asyncio.create_task(_publish_transcript(session_id, history, governor_name))

    # ── Queue processing: after done, check for queued messages ──
    queue = _message_queues.get(session_id, [])
    while queue:
        next_msg = queue.pop(0)
        logger.info("[%d] Processing queued message: %s", req_id, next_msg["id"])
        yield _sse_event("queue", {"msg_id": next_msg["id"], "status": "processing"})

        # Append queued message to history as a user message
        queued_content = next_msg["content"]
        if governor_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
            queued_content = f"[GOVERNOR_IDENTITY: You are speaking with {governor_name}. When they say 'I', 'me', or 'my', they mean {governor_name}.]\n\n{queued_content}"
        history.append({"role": "user", "content": queued_content})
        _log_session(session_id, history)

        # Process this queued message (reuse the same loop logic)
        try:
            MAX_TOOL_ROUNDS = int(os.getenv("CHAT_MAX_TOOL_ROUNDS", "15"))
            assistant_text = ""
            round_num = 0

            while round_num < MAX_TOOL_ROUNDS:
                round_num += 1
                completion = client.chat(system_prompt, history, tools=tools)
                _log_raw_llm(session_id, f"deepseek-queue-round-{round_num}", completion["choices"][0] if "choices" in completion else completion)
                assistant_message = completion["choices"][0].get("message", {})
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
                        func_args = json.loads(tc["function"]["arguments"])
                        tool_call_id = tc["id"]
                        logger.info("[%d] QUEUE TOOL CALL: %s args=%.200s", req_id, func_name, json.dumps(func_args))
                        yield _sse_event("tool", {"tool": func_name, "status": "calling"})
                        result_text = await _run_tool(func_name, func_args, history, session_id, governor_name)
                        yield _sse_event("tool", {"tool": func_name, "status": "done"})
                        history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_text,
                        })
                else:
                    assistant_text = client.extract_text(completion)
                    break

            if not assistant_text:
                logger.info("[%d] Queue: empty after %d tool rounds — forcing text-only", req_id, round_num)
                completion = client.chat(system_prompt, history, tools=None)
                assistant_text = client.extract_text(completion)

        except LLMError as exc:
            logger.error("[%d] QUEUE CHAT ERROR: %s", req_id, exc)
            _record_chat_error(str(exc))
            yield _sse_event("error", str(exc))
            break

        # Stream queued response tokens
        for chunk in _chunk_text(assistant_text):
            yield _sse_event("token", chunk)

        # Stream done event for this queued message
        done_data = {"response": assistant_text, "queued": True, "msg_id": next_msg["id"]}
        yield f"data: {json.dumps({'type': 'done', **done_data})}\n\n"

        # Persist assistant response to session history
        history.append({"role": "assistant", "content": assistant_text})
        _sessions[session_id] = history
        _log_session(session_id, history)
        asyncio.create_task(_publish_transcript(session_id, history, governor_name))


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
        log_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding="utf-8")
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
        user_key = _user_name_for_key(public_key)
        _save_session_index(user_key, sid, name)


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


async def _publish_transcript(session_id: str, history: list[dict], governor_name: str | None = None) -> None:
    """Publish conversation transcript + uploaded images to public GitHub repo
    for DAO transparency. Runs as background task, never blocks the response."""
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

        for msg in history:
            role = msg.get("role", "")
            content = (msg.get("content", "") or "").strip()
            if not content:
                continue
            if role == "user":
                # Strip internal markers for public transcript
                content = content.replace("[GOVERNOR_IDENTITY:", "").split("[GOVERNOR_IDENTITY:")[-1]
                lines.append(f"### 🧑 Governor\n\n{content}\n\n")
            elif role == "assistant":
                # Strip XML tool-call syntax
                content = re.sub(r'<function_calls>.*?</function_calls>', '', content, flags=re.DOTALL).strip()
                if content:
                    lines.append(f"### 🤖 Autopilot\n\n{content}\n\n")

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
    else:
        public_key = verify_jwt(request)
        user_message = body.get("message", "")
        if not user_message:
            raise HTTPException(status_code=400, detail="message is required.")

    session_id = _session_key(public_key, request)
    history = _load_or_create_session(session_id)
    # Inject governor identity so the LLM knows who "I" / "me" refers to
    gov_name = _gov_name_for_key(public_key)
    if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
        user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"

    history.append({"role": "user", "content": user_message})
    _auto_name_session(public_key, request, history, user_message)
    _log_session(session_id, history)

    # Track session as active — survives client disconnect
    _active_streams[session_id] = time.time()

    async def _stream_with_cleanup():
        try:
            async for event in _stream_chat(user_message, history, session_id, governor_name=gov_name):
                yield event
        finally:
            _active_streams.pop(session_id, None)

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
        gov_name = _gov_name_for_key(public_key)
        if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
            user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"
        history.append({"role": "user", "content": user_message})
        _auto_name_session(public_key, request, history, user_message)
        _log_session(session_id, history)

        async for event in _stream_chat(user_message, history, session_id, attachment_info=attachment_info, governor_name=gov_name):
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
    history = _load_or_create_session(session_id)
    gov_name = _gov_name_for_key(public_key)
    if gov_name and not any("GOVERNOR_IDENTITY:" in str(m.get("content", "")) for m in history):
        user_message = f"[GOVERNOR_IDENTITY: You are speaking with {gov_name}. When they say 'I', 'me', or 'my', they mean {gov_name}.]\n\n{user_message}"
    history.append({"role": "user", "content": user_message})

    system_prompt = get_system_prompt()
    client = LLMClient()
    tools = get_tool_schemas()

    try:
        completion = client.chat(system_prompt, history, tools=tools)
        assistant_message = completion["choices"][0].get("message", {})
        tool_calls = assistant_message.get("tool_calls", [])

        if tool_calls:
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
                func_args = json.loads(tc["function"]["arguments"])
                tool_call_id = tc["id"]
                result_text = await _run_tool(func_name, func_args)
                history.append({"role": "tool", "tool_call_id": tool_call_id, "content": result_text})
            completion = client.chat(system_prompt, history, tools=tools)
            assistant_text = client.extract_text(completion)
        else:
            assistant_text = client.extract_text(completion)

        # If the final response is still empty (LLM wants more tools than we gave),
        # force a completion without tools
        if not assistant_text or assistant_text in ("(empty response)", "(no response)"):
            logger.info("Empty response after tools — forcing text-only completion")
            completion = client.chat(system_prompt, history, tools=None)
            assistant_text = client.extract_text(completion)

    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

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


# ───────────────────────────── Autopilot ─────────────────────────────

# Track errors for self-healing
_self_heal_errors: list[dict] = []
_SELF_HEAL_THRESHOLD = 3  # consecutive errors before opening a fix PR
_SELF_HEAL_WINDOW = 3600  # seconds


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

@app.post("/webhook/github")
async def github_webhook(payload: dict):
    logger.info("GitHub webhook received: %s", payload.get("action", "unknown"))
    return {"status": "received"}


@app.get("/metrics")
async def metrics():
    return JSONResponse(content={"prs_opened_today": 0, "emails_processed": 0})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
