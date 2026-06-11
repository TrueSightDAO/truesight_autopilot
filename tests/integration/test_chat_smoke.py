#!/usr/bin/env python3
"""Smoke test: one-tool-call chat round-trip.

Sends a chat that requires exactly one tool call (`list_org_repos`) and
asks for a single-line numeric answer. Asserts:
  - HTTP 200 + clean SSE stream
  - At least one `tool` event for list_org_repos
  - A `done` event with a non-empty response

This is the cheapest end-to-end signal that the chat path is alive,
that tool dispatch works, and that the configured LLM provider responds.
"""

from __future__ import annotations

import asyncio
import sys

from _autopilot_client import (
    DEFAULT_AUTOPILOT_URL,
    GovernorKey,
    banner,
    report,
    require_running_autopilot,
    stream_chat,
)

PROMPT = (
    "Use the `read_repo_file` tool to fetch repo=truesight_autopilot path=README.md "
    "(this is mandatory — do NOT skip the tool call). Then reply with a single line "
    "containing the FIRST four words of the README in lowercase, separated by spaces, "
    "with no punctuation. No prose."
)


async def run() -> int:
    banner(f"chat smoke — {DEFAULT_AUTOPILOT_URL}")
    require_running_autopilot()
    key = GovernorKey()

    seen_tool = False

    def on_event(t: str, data: dict) -> None:
        nonlocal seen_tool
        if (
            t == "tool"
            and data.get("tool") == "read_repo_file"
            and data.get("status") == "calling"
        ):
            seen_tool = True
            print("  → tool: read_repo_file calling", flush=True)
        elif t == "heartbeat":
            print(
                f"  → heartbeat phase={data.get('phase')} elapsed={data.get('elapsed_s')}s",
                flush=True,
            )

    result = await stream_chat(key, PROMPT, on_event=on_event)

    fails = 0
    fails += report("HTTP 200 + done received", result["saw_done"])
    fails += report("read_repo_file tool was called", seen_tool)
    fails += report(
        "final response is non-empty",
        bool((result["final_response"] or "").strip()),
        f"len={len(result['final_response'] or '')}",
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
