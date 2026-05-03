"""FastAPI application for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import create_jwt, verify_jwt, verify_payload
from .config import settings
from .context import get_system_prompt, refresh_system_prompt, get_context_file
from .governor_registry import refresh_cache as refresh_governor_cache, load_governors
from .llm_client import LLMClient, LLMError, get_tool_schemas
from .tools.github_tools import read_repo_file
from .email_poller import EmailPoller
from .aws_monitor import AWSMonitor

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger("autopilot")

email_poller: EmailPoller | None = None
aws_monitor: AWSMonitor | None = None
_sessions: dict[str, list[dict[str, str]]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global email_poller, aws_monitor
    logger.info("Autopilot starting up...")

    if not settings.dry_run:
        email_poller = EmailPoller()
        aws_monitor = AWSMonitor()
        asyncio.create_task(email_poller.run_loop())
        asyncio.create_task(aws_monitor.run_loop())
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


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """Main chat endpoint. Receives signed message or JWT-authenticated message."""
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
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": tc["type"],
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                func_args = json.loads(tc["function"]["arguments"])
                tool_call_id = tc["id"]

                if func_name == "read_context_file":
                    result = get_context_file(func_args.get("path", ""))
                    result_text = result if result else "File not found."
                elif func_name == "read_repo_file":
                    result = read_repo_file(
                        func_args.get("repo", ""),
                        func_args.get("path", ""),
                        func_args.get("ref", "main"),
                    )
                    if result.get("type") == "file":
                        result_text = result["content"]
                    elif result.get("type") == "directory":
                        result_text = "Directory listing:\n" + "\n".join(
                            f"- {e['name']} ({e['type']})" for e in result.get("entries", [])
                        )
                    else:
                        result_text = f"Error: {result.get('error', 'unknown')}"
                elif func_name == "open_fix_pr":
                    result_text = f"Autopilot fix requested for {func_args.get('repo')}. This will be processed asynchronously."
                    # TODO: enqueue fix task
                elif func_name == "create_dao_submission":
                    result_text = "DAO submission tool is not yet enabled. Please describe your work and I will help you compile it."
                else:
                    result_text = f"Unknown tool: {func_name}"

                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                })

            completion = client.chat(system_prompt, history, tools=tools)
            assistant_text = client.extract_text(completion)
        else:
            assistant_text = client.extract_text(completion)

    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

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
