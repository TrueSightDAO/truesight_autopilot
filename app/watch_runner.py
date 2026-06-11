"""Detached watch-and-notify poller.

Launched (as a `start_new_session` subprocess, like ``ci_pingback``) by the
``watch_aws_resource`` / ``watch_url`` tools. It outlives the bounded chat turn,
polls a long-running async operation to a terminal state, and posts a message
back to the *originating* Telegram topic — so Sophia's "I'll let you know when
it's done" is actually true instead of a promise nothing keeps.

It must NOT run inside a chat turn: the turn holds the per-topic executor lock
(see SOPHIA_THREAD_CONCURRENCY_PLAN.md), so a long block would freeze the topic.
A detached poller sidesteps that entirely.

Usage
-----
    python -m app.watch_runner --kind ami --resource-id ami-0123 \
        --account nelanco --region us-east-1 --chat-id -100123 --thread-id 5 \
        --label "getdata-cache AMI" [--interval 30] [--timeout 3600]

    python -m app.watch_runner --kind http --url https://edgar.truesight.me/ping \
        --expect-status 200 [--expect-substring ok] --chat-id -100123 \
        --thread-id 5 --label "Edgar deploy"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Reuse the proven Telegram sender from ci_pingback.
from app.ci_pingback import send_telegram

# ── AWS resource kinds: how to describe each and what "done"/"failed" mean ────
# state_path is resolved by _extract_aws_state below (instances nest differently).
_AWS_KINDS: dict[str, dict] = {
    "ami": {
        "service": "ec2",
        "operation": "DescribeImages",
        "ids_key": "ImageIds",
        "list_key": "Images",
        "state_field": "State",
        "done": {"available"},
        "failed": {"failed", "error", "invalid", "deregistered"},
    },
    "snapshot": {
        "service": "ec2",
        "operation": "DescribeSnapshots",
        "ids_key": "SnapshotIds",
        "list_key": "Snapshots",
        "state_field": "State",
        "done": {"completed"},
        "failed": {"error"},
    },
    "instance_running": {
        "service": "ec2",
        "operation": "DescribeInstances",
        "ids_key": "InstanceIds",
        "list_key": None,
        "state_field": "State.Name",  # Reservations[].Instances[]
        "done": {"running"},
        "failed": {"terminated", "stopped", "shutting-down"},
    },
    "volume": {
        "service": "ec2",
        "operation": "DescribeVolumes",
        "ids_key": "VolumeIds",
        "list_key": "Volumes",
        "state_field": "State",
        "done": {"available", "in-use"},
        "failed": {"error", "deleting", "deleted"},
    },
}

AWS_KINDS = frozenset(_AWS_KINDS)


def _resource_spec(kind: str) -> dict:
    if kind not in _AWS_KINDS:
        raise ValueError(f"unknown aws kind: {kind}")
    return _AWS_KINDS[kind]


def _extract_aws_state(kind: str, resp: dict) -> str | None:
    """Pull the state string out of a describe response. Returns None when the
    resource isn't present yet (eventual consistency → treat as still pending)."""
    spec = _resource_spec(kind)
    if not isinstance(resp, dict) or resp.get("status") == "error":
        return None
    if kind == "instance_running":
        for res in resp.get("Reservations", []) or []:
            for inst in res.get("Instances", []) or []:
                st = (inst.get("State") or {}).get("Name")
                if st:
                    return st
        return None
    items = resp.get(spec["list_key"]) or []
    if not items:
        return None
    return items[0].get(spec["state_field"])


def _classify_aws(kind: str, resp: dict) -> tuple[str, str | None]:
    """Return (status, state) where status ∈ {done, failed, pending}."""
    spec = _resource_spec(kind)
    state = _extract_aws_state(kind, resp)
    if state is None:
        return "pending", None
    if state in spec["done"]:
        return "done", state
    if state in spec["failed"]:
        return "failed", state
    return "pending", state


def _probe_aws(kind: str, resource_id: str, account: str, region: str | None) -> tuple[str, str | None]:
    # Imported lazily so unit tests of the pure logic don't require boto3/creds.
    from app.tools.aws_tools import aws_query

    spec = _resource_spec(kind)
    raw = aws_query(
        account=account,
        service=spec["service"],
        operation=spec["operation"],
        parameters={spec["ids_key"]: [resource_id]},
        region=region,
    )
    try:
        resp = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "pending", None
    # aws_query wraps the boto3 result: {"status": "ok", "response": {<describe>}}.
    # Unwrap before classifying; an error envelope → treat as still pending.
    if isinstance(resp, dict):
        if resp.get("status") == "error":
            return "pending", None
        if "response" in resp and isinstance(resp["response"], dict):
            resp = resp["response"]
    return _classify_aws(kind, resp)


def _probe_http(url: str, expect_status: int, expect_substring: str | None) -> tuple[str, str | None]:
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    try:
        with urlopen(url, timeout=15) as resp:  # noqa: S310 — operator-supplied health URL
            code = resp.getcode()
            body = resp.read(8192).decode("utf-8", "replace")
    except HTTPError as e:
        code, body = e.code, ""
    except (URLError, OSError):
        return "pending", None
    if code == expect_status and (not expect_substring or expect_substring in body):
        return "done", str(code)
    return "pending", str(code)


def _notify(chat_id: str, thread_id: str | None, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_API_KEY", "")
    if not token:
        print("[watch_runner] no TELEGRAM_BOT_API_KEY in env", file=sys.stderr)
        return
    send_telegram(token, chat_id, text, thread_id)


def run(args: argparse.Namespace) -> int:
    label = args.label or f"{args.kind} {getattr(args, 'resource_id', '') or args.url}"
    deadline = time.time() + args.timeout
    last_state: str | None = None

    def probe() -> tuple[str, str | None]:
        if args.kind == "http":
            return _probe_http(args.url, args.expect_status, args.expect_substring)
        return _probe_aws(args.kind, args.resource_id, args.account, args.region)

    while time.time() < deadline:
        status, state = probe()
        last_state = state or last_state
        if status == "done":
            target = getattr(args, "resource_id", "") or args.url
            _notify(
                args.chat_id,
                args.thread_id,
                f"✅ <b>{label}</b> is ready — {target}" + (f" reached state <code>{state}</code>." if state else "."),
            )
            return 0
        if status == "failed":
            _notify(
                args.chat_id,
                args.thread_id,
                f"❌ <b>{label}</b> failed — {args.resource_id} reached state <code>{state}</code>.",
            )
            return 0
        time.sleep(args.interval)

    mins = round(args.timeout / 60)
    _notify(
        args.chat_id,
        args.thread_id,
        f"⏳ <b>{label}</b> still not done after {mins}m"
        + (f" (last state: <code>{last_state}</code>)" if last_state else "")
        + ". Stopping watch — ping me to re-check.",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Detached watch-and-notify poller.")
    p.add_argument("--kind", required=True, choices=sorted(AWS_KINDS) + ["http"])
    p.add_argument("--resource-id", default="")
    p.add_argument("--account", default="")
    p.add_argument("--region", default=None)
    p.add_argument("--url", default="")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--expect-substring", default=None)
    p.add_argument("--chat-id", required=True)
    p.add_argument("--thread-id", default=None)
    p.add_argument("--label", default="")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
