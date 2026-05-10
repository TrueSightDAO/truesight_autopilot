#!/usr/bin/env python3
"""Abort/cancel: covers DELETE /chat/active/{session_short} from PR #29.

Strategy:
  1. Open a long /chat stream that chains five tool calls (so we have
     ~10–20 seconds of work to interrupt).
  2. After ~8s, fire DELETE /chat/active/{first16chars-of-pubkey}.
  3. Assert that the SSE stream emits a `cancelled` event with
     reason=user_requested and exits cleanly without a `done`.

This validates both the cancel-flag plumbing AND the next-round / next-
heartbeat detection in `_run_tool_round_loop`.
"""
from __future__ import annotations

import asyncio
import sys

from _autopilot_client import (
    DEFAULT_AUTOPILOT_URL,
    GovernorKey,
    banner,
    cancel_chat,
    report,
    require_running_autopilot,
    stream_chat,
)


INITIAL = (
    "Run these in order, one tool call each — DO NOT batch: "
    "list_org_repos, then read_repo_file repo=truesight_autopilot path=README.md, "
    "then read_repo_file repo=agentic_ai_context path=PROJECT_INDEX.md, "
    "then read_repo_file repo=dapp path=README.md, "
    "then read_repo_file repo=dao_client path=README.md, "
    "then summarize all five in one paragraph."
)
CANCEL_AFTER_S = 8.0


async def run() -> int:
    banner(f"chat cancel — {DEFAULT_AUTOPILOT_URL}")
    require_running_autopilot()
    key = GovernorKey()

    saw_cancelled = False
    cancel_reason = None

    def on_event(t: str, data: dict) -> None:
        nonlocal saw_cancelled, cancel_reason
        if t == "cancelled":
            saw_cancelled = True
            cancel_reason = data.get("reason")
            print(f"  → cancelled at round={data.get('round')} phase={data.get('phase')} reason={cancel_reason}", flush=True)
        elif t == "tool":
            print(f"  → tool {data.get('tool')} {data.get('status')}", flush=True)

    async def fire_cancel_after(delay: float) -> None:
        await asyncio.sleep(delay)
        resp = await cancel_chat(key)
        print(f"  → DELETE /chat/active → {resp['status_code']} {resp['body']}", flush=True)

    cancel_task = asyncio.create_task(fire_cancel_after(CANCEL_AFTER_S))
    result = await stream_chat(key, INITIAL, on_event=on_event)
    await cancel_task

    fails = 0
    fails += report("cancelled SSE event arrived", saw_cancelled)
    fails += report(
        "cancel reason was user_requested",
        cancel_reason == "user_requested",
        f"got: {cancel_reason!r}",
    )
    fails += report(
        "stream did NOT emit `done` (clean cancel exit)",
        not result["saw_done"],
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
