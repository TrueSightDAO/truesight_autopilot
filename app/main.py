"""FastAPI application for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import base64
import mimetypes
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import create_jwt, verify_jwt, verify_payload
from .config import settings
from .context import get_system_prompt, refresh_system_prompt, get_context_file
from .governor_registry import refresh_cache as refresh_governor_cache, load_governors
from .llm_client import LLMClient, LLMError, get_tool_schemas
from .tools.github_tools import read_repo_file
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
UPLOAD_DIR = Path("/tmp/autopilot_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


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
        "governors_count": len(gov_data.get("governors", [])),
        "governors_updated_at": gov_data.get("updated_at", ""),
    }


# ───────────────────────────── Governor Chat ─────────────────────────────

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


async def _run_tool(func_name: str, func_args: dict) -> str:
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
        edgar = EdgarDirectClient()
        event_name = func_args.get("event_name", "CONTRIBUTION EVENT")
        attributes = func_args.get("attributes", {})
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
        return f"PR opened: {pr_url}" if pr_url else "Fix agent failed to produce a PR."
    if func_name == "create_dao_submission":
        return "DAO submission tool is not yet enabled. Please describe your work and I will help you compile it."
    return f"Unknown tool: {func_name}"


async def _stream_chat(user_message: str, history: list[dict], session_id: str, attachment_info: dict | None = None):
    system_prompt = get_system_prompt()
    client = LLMClient()
    tools = get_tool_schemas()
    req_id = int(time.time() * 1000) % 1000000
    logger.info("[%d] CHAT REQ: session=%s msg=%.150s attach=%s", req_id, session_id[:16], user_message, attachment_info is not None)

    try:
        completion = client.chat(system_prompt, history, tools=tools)
        assistant_message = completion["choices"][0].get("message", {})
        logger.info("[%d] DEEPSEEK RESP: tools=%d tokens=%s", req_id,
                     len(assistant_message.get("tool_calls", [])),
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

                # Announce tool call
                yield _sse_event("tool", {"tool": func_name, "status": "calling"})

                result_text = await _run_tool(func_name, func_args)

                yield _sse_event("tool", {"tool": func_name, "status": "done"})
                logger.info("[%d] TOOL RESULT: %s result=%.300s", req_id, func_name, result_text[:300])

                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                })

            # Get final response after tool calls
            completion = client.chat(system_prompt, history, tools=tools)
            assistant_text = client.extract_text(completion)
        else:
            assistant_text = client.extract_text(completion)

    except LLMError as exc:
        logger.error("[%d] CHAT ERROR: %s", req_id, exc)
        _record_chat_error(str(exc))
        yield _sse_event("error", str(exc))
        return

    # Log final response
    logger.info("[%d] CHAT RESP: len=%d tokens=%.150s", req_id, len(assistant_text), assistant_text[:150])

    # Parse embedded proposal JSON
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

    # Stream final response tokens
    for chunk in _chunk_text(assistant_text):
        yield _sse_event("token", chunk)

    # Stream done event
    done_data: dict[str, object] = {"response": assistant_text}
    if proposal:
        done_data["proposal"] = proposal
    yield f"data: {json.dumps({'type': 'done', **done_data})}\n\n"


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

    session_id = public_key
    history = _sessions.get(session_id, [])
    history.append({"role": "user", "content": user_message})

    return StreamingResponse(
        _stream_chat(user_message, history, session_id),
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
    file: UploadFile | None = File(None),
):
    """Accepts a chat message + optional file attachment. Saves the file to /tmp
    and includes a reference in the conversation so the LLM can analyze it."""
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

    session_id = public_key
    attachment_info = None

    if file and file.filename:
        ext = Path(file.filename).suffix or ""
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / safe_name
        content = await file.read()
        dest.write_bytes(content)

        mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
        size_kb = round(len(content) / 1024, 1)

        # Convert HEIC/HEIF to JPEG for broader compatibility
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
                converted = True
                logger.info("Converted HEIC %s to JPEG (%d KB)", file.filename, size_kb)
            except Exception as e:
                logger.warning("HEIC conversion failed for %s: %s", file.filename, e)

        attachment_info = {
            "filename": file.filename,
            "saved_as": str(dest),
            "mime_type": mime_type,
            "size_kb": size_kb,
        }

        # Build a descriptive message for the LLM
        content_part = (
            f"[File attachment: {file.filename} ({mime_type}, {size_kb} KB)]\n"
            f"File saved at: {dest}\n"
        )
        if converted:
            content_part += f"(Converted from HEIC to JPEG for analysis)\n"

        # For images under 5MB, include base64 preview for vision-capable models
        if mime_type and mime_type.startswith("image/") and len(content) < 5 * 1024 * 1024:
            b64 = base64.b64encode(content).decode()
            b64_data_url = f"data:{mime_type};base64,{b64}"
            content_part += f"Image data URL: {b64_data_url}\n"

        user_message = f"{user_message_text}\n\n{content_part}" if user_message_text.strip() else content_part.strip()
    else:
        user_message = user_message_text

    if not user_message.strip() and not user_message_text.strip():
        raise HTTPException(status_code=400, detail="message or file is required.")

    history = _sessions.get(session_id, [])
    history.append({"role": "user", "content": user_message})

    return StreamingResponse(
        _stream_chat(user_message, history, session_id, attachment_info=attachment_info),
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

    session_id = public_key
    history = _sessions.get(session_id, [])
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
