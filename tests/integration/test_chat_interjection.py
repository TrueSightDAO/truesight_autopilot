#!/usr/bin/env python3
"""Mid-round queue interjection: covers the SSE heartbeat + the
mid-round queue peek shipped in PR #28.

Strategy:
  1. Open a long /chat stream that triggers multiple tool calls (so we
     have time to interject between rounds).
  2. After ~6s, POST a follow-up to /chat/queue containing the literal
     instruction "reply with the token FOLLOWUP_NOTICED on its own line".
  3. Assert that:
       - the SSE stream emits an `interjected` event with the queued msg_id
       - the LLM's response on a subsequent round contains FOLLOWUP_NOTICED

The presence of FOLLOWUP_NOTICED in the response confirms the LLM actually
saw the interjected message in-context (not just that the queue plumbing
fired).
"""

from __future__ import annotations

import asyncio
import sys

from _autopilot_client import (
    DEFAULT_AUTOPILOT_URL,
    GovernorKey,
    banner,
    queue_message,
    report,
    require_running_autopilot,
    stream_chat,
)

INITIAL = (
    "Run these in order: list_org_repos, then read_repo_file repo=truesight_autopilot "
    "path=README.md, then reply with a single line `count: <number_of_repos>`."
)
INTERJECTION = "INTERJECTION TEST: reply NOW with the literal string FOLLOWUP_NOTICED on its own line, then continue."
INTERJECT_AFTER_S = 6.0


async def run() -> int:
    banner(f"chat interjection — {DEFAULT_AUTOPILOT_URL}")
    require_running_autopilot()
    key = GovernorKey()

    seen_interjection = False
    seen_followup_token = False

    def on_event(t: str, data: dict) -> None:
        nonlocal seen_interjection, seen_followup_token
        if t == "queue" and data.get("status") == "interjected":
            seen_interjection = True
            print(f"  → interjected msg_id={data.get('msg_id')}", flush=True)
        elif t == "token" and "FOLLOWUP_NOTICED" in (data.get("content") or ""):
            seen_followup_token = True
            print("  → FOLLOWUP_NOTICED appeared in token stream", flush=True)
        elif t == "tool":
            print(f"  → tool {data.get('tool')} {data.get('status')}", flush=True)

    async def fire_interjection_after(delay: float) -> None:
        await asyncio.sleep(delay)
        resp = await queue_message(key, INTERJECTION)
        print(f"  → POST /chat/queue → {resp}", flush=True)

    interject_task = asyncio.create_task(fire_interjection_after(INTERJECT_AFTER_S))
    result = await stream_chat(key, INITIAL, on_event=on_event)
    await interject_task

    # Belt-and-suspenders: also accept FOLLOWUP_NOTICED in the final response
    # in case the token chunk boundary swallowed the substring above.
    final = result["final_response"] or ""
    if "FOLLOWUP_NOTICED" in final:
        seen_followup_token = True

    fails = 0
    fails += report("interjected SSE event arrived", seen_interjection)
    fails += report(
        "LLM acknowledged interjection (FOLLOWUP_NOTICED in response)",
        seen_followup_token,
        f"final_len={len(final)}",
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
