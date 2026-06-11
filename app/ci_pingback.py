#!/usr/bin/env python3
"""
CI Pingback Monitor — polls a GitHub Actions workflow run and sends a
Telegram notification to a specified chat/thread when it completes.

Usage:
    python -m app.ci_pingback \
        --repo TrueSightDAO/dao_protocol \
        --run-id <run_id> \
        --chat-id -1003919341801 \
        --thread-id 1776 \
        --poll-interval 30

Requires:
    - GITHUB_TOKEN in env (for API auth)
    - TELEGRAM_BOT_TOKEN in env (for sending messages)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_run(repo: str, run_id: int, token: str) -> dict:
    """Fetch a single workflow run from the GitHub API."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "truesight-autopilot-ci-pingback")

    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500] if e.fp else ""
        print(f"[ci_pingback] HTTP {e.code} fetching run: {body}", file=sys.stderr)
        return {}
    except URLError as e:
        print(f"[ci_pingback] Network error: {e.reason}", file=sys.stderr)
        return {}


def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    thread_id: str | None = None,
) -> bool:
    """Send a message via Telegram Bot API."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        print(f"[ci_pingback] Telegram send failed: {e}", file=sys.stderr)
        return False


def format_duration(started: str, completed: str) -> str:
    """Format duration between two ISO timestamps."""
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        delta = e - s
        total_secs = int(delta.total_seconds())
        if total_secs < 60:
            return f"{total_secs}s"
        return f"{total_secs // 60}m {total_secs % 60}s"
    except (ValueError, AttributeError):
        return "?"


def build_message(run: dict) -> str:
    """Build a Telegram message from a completed workflow run."""
    conclusion = run.get("conclusion", "unknown")
    name = run.get("display_title", run.get("name", "Workflow"))
    html_url = run.get("html_url", "")
    started = run.get("run_started_at", "")
    completed = run.get("updated_at", "")
    duration = format_duration(started, completed)

    if conclusion == "success":
        emoji = "✅"
        status_text = "succeeded"
    elif conclusion == "failure":
        emoji = "❌"
        status_text = "failed"
    elif conclusion == "cancelled":
        emoji = "🚫"
        status_text = "was cancelled"
    else:
        emoji = "⚠️"
        status_text = conclusion

    lines = [
        f"{emoji} CI <b>{name}</b> {status_text}",
        "",
        f"Duration: {duration}",
        f"<a href='{html_url}'>View run →</a>" if html_url else "",
    ]
    return "\n".join(line for line in lines if line)


def main():
    parser = argparse.ArgumentParser(description="CI Pingback Monitor")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/name)")
    parser.add_argument("--run-id", required=True, type=int, help="Workflow run ID")
    parser.add_argument("--chat-id", required=True, help="Telegram chat ID")
    parser.add_argument("--thread-id", help="Telegram thread/topic ID")
    parser.add_argument(
        "--poll-interval", type=int, default=30, help="Poll interval in seconds"
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN or GH_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    print(f"[ci_pingback] Monitoring {args.repo}/actions/runs/{args.run_id}...")
    print(
        f"[ci_pingback] Polling every {args.poll_interval}s, will notify chat {args.chat_id}"
    )

    last_status = None

    while True:
        run = fetch_run(args.repo, args.run_id, token)
        status = run.get("status", "")

        if not status:
            print("[ci_pingback] Could not fetch run status, retrying...")
            time.sleep(args.poll_interval)
            continue

        if status != last_status:
            print(f"[ci_pingback] Status: {status}")
            last_status = status

        if status == "completed":
            conclusion = run.get("conclusion", "unknown")
            print(f"[ci_pingback] Run completed: {conclusion}")

            message = build_message(run)
            ok = send_telegram(bot_token, args.chat_id, message, args.thread_id)

            if ok:
                print("[ci_pingback] Notification sent ✓")
            else:
                print("[ci_pingback] Failed to send notification", file=sys.stderr)
                sys.exit(1)

            sys.exit(0 if conclusion == "success" else 1)

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
