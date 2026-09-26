"""watch_aws_resource / watch_url — register a detached watch-and-notify poller.

These let Sophia keep her word. A chat turn is bounded (the adapter caps it at
180s) and holds the per-topic executor lock, so she can NOT block-wait on a slow
op (AMI bake, instance boot, deploy). Instead she calls one of these tools, which
launches a detached ``app.watch_runner`` process that polls the op and posts back
to *this* Telegram topic when it finishes. ONLY after calling one of these may she
truthfully say "I'll let you know when it's done."
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ..watch_runner import AWS_KINDS

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _chat_thread_from_session(session_id: str | None) -> tuple[str | None, str | None]:
    """Parse ``tg:<chat>:<thread>`` → (chat_id, thread_id). thread '0' (no forum
    topic) → None. Non-Telegram sessions → (None, None)."""
    if not session_id or not session_id.startswith("tg:"):
        return None, None
    parts = session_id.split(":")
    if len(parts) < 3:
        return None, None
    chat_id = parts[1]
    thread_id = parts[2]
    if thread_id in ("", "0"):
        thread_id = None
    return chat_id, thread_id


def _launch(argv: list[str]) -> None:
    """Spawn the poller detached so it survives the turn (start_new_session) and
    inherits the service env (AWS creds + TELEGRAM_BOT_API_KEY)."""
    subprocess.Popen(  # noqa: S603 — fixed argv, operator-trusted
        [sys.executable, "-m", "app.watch_runner", *argv],
        cwd=_REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def watch_aws_resource(args: dict, ctx: dict) -> str:
    chat_id, thread_id = _chat_thread_from_session(ctx.get("session_id"))
    if chat_id is None:
        return json.dumps(
            {
                "status": "error",
                "reason": "watch_aws_resource only works inside a Telegram topic.",
            }
        )
    kind = args.get("resource_kind", "")
    rid = args.get("resource_id", "")
    account = args.get("account", "")
    if kind not in AWS_KINDS:
        return json.dumps(
            {
                "status": "error",
                "reason": f"resource_kind must be one of {sorted(AWS_KINDS)}",
            }
        )
    if not rid or not account:
        return json.dumps(
            {"status": "error", "reason": "resource_id and account are required."}
        )
    label = args.get("label") or f"{kind} {rid}"
    interval = int(args.get("interval_seconds", 30))
    timeout = int(args.get("timeout_seconds", 3600))
    argv = [
        "--kind",
        kind,
        "--resource-id",
        rid,
        "--account",
        account,
        "--chat-id",
        chat_id,
        "--label",
        label,
        "--interval",
        str(interval),
        "--timeout",
        str(timeout),
    ]
    if args.get("region"):
        argv += ["--region", args["region"]]
    if thread_id is not None:
        argv += ["--thread-id", thread_id]
    _launch(argv)
    return json.dumps(
        {
            "status": "watching",
            "resource": rid,
            "message": f"👁 Watching {label} ({rid}). I'll post here as soon as it's done "
            f"(checking every {interval}s, up to {round(timeout / 60)}m).",
        }
    )


def watch_url(args: dict, ctx: dict) -> str:
    chat_id, thread_id = _chat_thread_from_session(ctx.get("session_id"))
    if chat_id is None:
        return json.dumps(
            {
                "status": "error",
                "reason": "watch_url only works inside a Telegram topic.",
            }
        )
    url = args.get("url", "")
    if not url:
        return json.dumps({"status": "error", "reason": "url is required."})
    label = args.get("label") or url
    interval = int(args.get("interval_seconds", 30))
    timeout = int(args.get("timeout_seconds", 1800))
    argv = [
        "--kind",
        "http",
        "--url",
        url,
        "--chat-id",
        chat_id,
        "--label",
        label,
        "--expect-status",
        str(int(args.get("expect_status", 200))),
        "--interval",
        str(interval),
        "--timeout",
        str(timeout),
    ]
    if args.get("expect_substring"):
        argv += ["--expect-substring", args["expect_substring"]]
    if thread_id is not None:
        argv += ["--thread-id", thread_id]
    _launch(argv)
    return json.dumps(
        {
            "status": "watching",
            "url": url,
            "message": f"👁 Watching {label}. I'll post here when it returns "
            f"{int(args.get('expect_status', 200))} (checking every {interval}s).",
        }
    )


from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="watch_aws_resource",
        description=(
            "Register a background watcher for a long-running AWS operation and get "
            "notified in THIS Telegram topic when it finishes. Use this right after "
            "you start something slow (an AMI/snapshot bake, an instance boot) so you "
            "can truthfully tell the user you'll report back — a chat turn is bounded "
            "and CANNOT wait for a multi-minute operation, so DO NOT promise to follow "
            "up unless you've called this. It returns immediately; a detached poller "
            "checks the resource until it reaches a terminal state, then posts here."
        ),
        parameters={
            "type": "object",
            "properties": {
                "resource_kind": {
                    "type": "string",
                    "enum": sorted(AWS_KINDS),
                    "description": "ami (snapshot bake), snapshot, instance_running (boot), volume.",
                },
                "resource_id": {
                    "type": "string",
                    "description": "e.g. ami-…, snap-…, i-…, vol-…",
                },
                "account": {
                    "type": "string",
                    "enum": ["explorya", "nelanco"],
                    "description": "AWS account label.",
                },
                "region": {
                    "type": "string",
                    "description": "Override the account default region (e.g. us-east-1).",
                },
                "label": {
                    "type": "string",
                    "description": "Human label for the notification, e.g. 'getdata-cache AMI'.",
                },
                "interval_seconds": {
                    "type": "integer",
                    "default": 30,
                    "description": "Poll interval.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 3600,
                    "description": "Give up + report after this long.",
                },
            },
            "required": ["resource_kind", "resource_id", "account"],
        },
        handler=lambda args, ctx: watch_aws_resource(args, ctx),
    ),
    ToolSpec(
        name="watch_url",
        description=(
            "Register a background watcher that polls a URL until it returns an "
            "expected status (and optional body substring), then notifies THIS "
            "Telegram topic. Use for 'tell me when the deploy/health endpoint is up'. "
            "Returns immediately; only promise to follow up after calling this."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to poll (e.g. a /ping or /health endpoint).",
                },
                "expect_status": {
                    "type": "integer",
                    "default": 200,
                    "description": "HTTP status that means 'done'.",
                },
                "expect_substring": {
                    "type": "string",
                    "description": "Optional: body must contain this too.",
                },
                "label": {
                    "type": "string",
                    "description": "Human label for the notification.",
                },
                "interval_seconds": {"type": "integer", "default": 30},
                "timeout_seconds": {"type": "integer", "default": 1800},
            },
            "required": ["url"],
        },
        handler=lambda args, ctx: watch_url(args, ctx),
    ),
]
