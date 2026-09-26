"""Thread-goal tools for the goal-anchored auto-advance loop (Track A / unit A2).

See ``agentic_ai_context/plans/SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md`` section 4.

These two tools let Sophia **declare** a thread's goal and **declare** it done --
rule 1 of the plan: no extra LLM call ever asks "are we done?". The primitive
they write to is :mod:`app.thread_goal` (unit A1); the loop that consumes it
lives behind ``GOAL_LOOP_ENABLED`` in ``main._compute_advance_signal`` +
``telegram_adapter._run_turn_with_auto_advance``.

Both resolve ``session_id`` from the tool context (the same session key the
brain uses for the advance signal), so a goal set in one thread can never drive
another (rule 5). Both are no-ops that return a clear status when the flag is
off, so exposing them is safe before the soak.
"""

from __future__ import annotations

import json
import logging

from ..config import settings
from ..tool_registry import ToolSpec
from .. import thread_goal

logger = logging.getLogger("autopilot.tools.thread_goal_tools")


def _session_from(ctx: dict) -> str | None:
    """The session key the brain keys goals on. None -> the tool fails closed."""
    sid = (ctx or {}).get("session_id")
    return sid or None


def set_thread_goal(
    goal_text: str, done_criteria: str = "", session_id: str | None = None
) -> str:
    """Open (or replace) this thread's goal. Returns the stored goal as JSON."""
    sid = session_id
    if not sid:
        return json.dumps(
            {
                "status": "error",
                "reason": "no session_id in context -- cannot key a goal (fail-closed)",
            }
        )
    if not goal_text or not goal_text.strip():
        return json.dumps({"status": "error", "reason": "goal_text is required"})
    g = thread_goal.open_goal(sid, goal_text.strip(), (done_criteria or "").strip())
    if g is None:
        return json.dumps({"status": "error", "reason": "goal could not be stored"})
    logger.info("thread-goal set: session=%s goal=%r", sid, g.goal_text)
    return json.dumps(
        {
            "status": "ok",
            "goal_loop_enabled": settings.goal_loop_enabled,
            "goal": {
                "goal_text": g.goal_text,
                "done_criteria": g.done_criteria,
                "status": g.status,
                "turns": g.turns,
            },
            "note": (
                "Goal recorded. Auto-continue will run it step-by-step once "
                "GOAL_LOOP_ENABLED is on; call complete_thread_goal when it is met."
            ),
        }
    )


def complete_thread_goal(session_id: str | None = None) -> str:
    """Mark this thread's goal complete. Returns the final state as JSON."""
    sid = session_id
    if not sid:
        return json.dumps(
            {
                "status": "error",
                "reason": "no session_id in context -- nothing to complete",
            }
        )
    before = thread_goal.current_goal(sid)
    if before is None:
        return json.dumps(
            {
                "status": "ok",
                "note": "no goal was set for this thread -- nothing to complete",
            }
        )
    g = thread_goal.complete_goal(sid)
    logger.info("thread-goal complete: session=%s goal=%r", sid, before.goal_text)
    return json.dumps(
        {
            "status": "ok",
            "goal": {
                "goal_text": (g.goal_text if g else before.goal_text),
                "status": (g.status if g else thread_goal.GOAL_COMPLETED),
                "turns": (g.turns if g else before.turns),
            },
            "note": "Goal marked complete -- auto-continue will stop for this thread.",
        }
    )


TOOL_SPECS = [
    ToolSpec(
        name="set_thread_goal",
        description=(
            "Record this conversation thread's goal so the autopilot can keep "
            "working toward it across turns without a new prompt each time. Call "
            "this the moment a governor states a task that will take more than one "
            "turn. Pass a short imperative goal_text and, when you can, a concrete "
            "done_criteria. Re-calling replaces the previous goal. Set the goal "
            "only for THIS thread; it can never advance another thread."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal_text": {
                    "type": "string",
                    "description": "Short imperative statement of the goal.",
                },
                "done_criteria": {
                    "type": "string",
                    "description": "How you will know the goal is met (optional).",
                },
            },
            "required": ["goal_text"],
        },
        handler=lambda args, ctx: set_thread_goal(
            args.get("goal_text", ""),
            args.get("done_criteria", ""),
            session_id=_session_from(ctx),
        ),
    ),
    ToolSpec(
        name="complete_thread_goal",
        description=(
            "Declare this thread's stated goal DONE, so the autopilot stops "
            "auto-advancing it. Call this only when the goal's done criteria are "
            "actually met -- it is the signal that ends the loop. Safe to call when "
            "no goal was set (it reports 'nothing to complete')."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args, ctx: complete_thread_goal(session_id=_session_from(ctx)),
    ),
]
