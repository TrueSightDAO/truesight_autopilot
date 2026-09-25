"""Per-thread goals for the goal-anchored auto-advance loop (Track A, unit A1).

See ``agentic_ai_context/plans/SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md`` section 4.

Today's auto-advance keys on a handoff *plan file*'s ``RESUME HERE`` + ``Advance``
markers (:mod:`app.auto_advance`) -- so a thread whose task is not a tracked
roadmap gets **no** auto-continue at all (roadmap gap G1, the single biggest
reason Gary still hand-drives). This module adds the missing primitive: a
lightweight, persisted, per-thread **goal** that Sophia writes when a governor
states a task and clears when she judges it done. The turn driver (wired in A2,
behind ``GOAL_LOOP_ENABLED``) then continues while a goal is OPEN and the last
turn made progress.

Design rules (from the roadmap, non-negotiable):

1. **Cheap** -- no extra LLM call; completion is *declared* by Sophia via a
   ``complete_thread_goal`` tool call, never inferred by a second model pass.
2. **Fail-closed** -- any error / missing state means *no* auto-continue.
3. **Flag-gated, lands dark** -- A1 is the pure primitive; nothing is wired yet.
4. **Always-stop preserved** -- reuse :data:`app.auto_advance._ALWAYS_STOP_RE`,
   one source of truth rather than a second regex.
5. **No cross-thread bleed** -- keyed to ``session_id``.
6. **Hard ceiling + stall** -- a per-goal turn ceiling and a stall detector so
   "keep going" cannot run away.

A1 scope is deliberately the store + :func:`should_continue` only. No wiring.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.auto_advance import _always_stop_reason

log = logging.getLogger(__name__)

GOAL_OPEN = "open"
GOAL_COMPLETED = "completed"

# A3 wires CHAT_MAX_GOAL_TURNS to this; A1 keeps the ceiling a plain parameter so
# the primitive stays pure and unit-testable.
DEFAULT_MAX_GOAL_TURNS = 40

_STATE_FILENAME = "thread_goals.jsonl"


def _state_path() -> Path:
    """Default JSONL path: under the deploy-watcher state dir (survives deploys)."""
    base = (
        os.getenv("THREAD_GOAL_STATE_DIR")
        or os.getenv("DEPLOY_WATCHER_STATE_DIR")
        or "data"
    )
    return Path(base) / _STATE_FILENAME


@dataclass(frozen=True)
class ThreadGoal:
    """A thread's goal and its running tallies.

    ``turns`` counts turns taken *while this goal was open*; ``last_progress_ts``
    is updated only by a turn that made real progress (so a stall is detectable).
    """

    session_id: str
    goal_text: str = ""
    done_criteria: str = ""
    status: str = GOAL_OPEN
    opened_ts: float = 0.0
    updated_ts: float = 0.0
    turns: int = 0
    last_progress_ts: float = 0.0


@dataclass(frozen=True)
class GoalDecision:
    """Outcome of :func:`should_continue`.

    ``decision`` is one of ``"continue"`` | ``"stop"`` | ``"done"``; ``reason``
    carries the human-readable stop reason (``None`` for continue/done).
    """

    decision: str
    reason: str | None = None


def _append_event(event: dict, *, path: Path | None = None) -> None:
    """Append one JSONL event. Fail-closed: a write error is logged, not raised."""
    target = path or _state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:  # pragma: no cover - defensive
        log.warning(
            "thread_goal: could not persist event %s: %s", event.get("event"), exc
        )


def _iter_events(session_id: str, *, path: Path | None = None) -> list[dict]:
    """Replay the JSONL, returning this session's events in order.

    Malformed lines are skipped (fail-closed) so one bad write cannot wedge a
    thread's goal state.
    """
    target = path or _state_path()
    out: list[dict] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("session_id") == session_id:
            out.append(ev)
    return out


def current_goal(session_id: str, *, path: Path | None = None) -> ThreadGoal | None:
    """Reconstruct the current goal for ``session_id`` from the event log.

    Returns ``None`` when no goal has ever been opened for that session. A later
    ``open`` supersedes an earlier one; ``complete`` is sticky until the next
    ``open`` (a stray ``turn`` after completion must not resurrect the goal).
    """
    if not session_id:
        return None
    events = _iter_events(session_id, path=path)
    if not events:
        return None
    goal: ThreadGoal | None = None
    for ev in events:
        kind = ev.get("event")
        ts = ev.get("ts", 0.0)
        if kind == "open":
            goal = ThreadGoal(
                session_id=session_id,
                goal_text=ev.get("goal_text", ""),
                done_criteria=ev.get("done_criteria", ""),
                status=GOAL_OPEN,
                opened_ts=ts,
                updated_ts=ts,
            )
        elif goal is None:
            continue
        elif kind == "turn":
            goal = ThreadGoal(
                session_id=session_id,
                goal_text=goal.goal_text,
                done_criteria=goal.done_criteria,
                status=goal.status,
                opened_ts=goal.opened_ts,
                updated_ts=ts,
                turns=goal.turns + 1,
                last_progress_ts=ts
                if ev.get("made_progress")
                else goal.last_progress_ts,
            )
        elif kind == "complete":
            goal = ThreadGoal(
                session_id=session_id,
                goal_text=goal.goal_text,
                done_criteria=goal.done_criteria,
                status=GOAL_COMPLETED,
                opened_ts=goal.opened_ts,
                updated_ts=ts,
                turns=goal.turns,
                last_progress_ts=goal.last_progress_ts,
            )
    return goal


def open_goal(
    session_id: str,
    goal_text: str,
    done_criteria: str = "",
    *,
    now: float | None = None,
    path: Path | None = None,
) -> ThreadGoal | None:
    """Open (or replace) a thread's goal. Returns the resulting goal, or None."""
    if not session_id or not goal_text:
        return None
    ts = time.time() if now is None else now
    _append_event(
        {
            "event": "open",
            "session_id": session_id,
            "goal_text": goal_text,
            "done_criteria": done_criteria,
            "ts": ts,
        },
        path=path,
    )
    return current_goal(session_id, path=path)


def record_turn(
    session_id: str,
    *,
    made_progress: bool,
    now: float | None = None,
    path: Path | None = None,
) -> ThreadGoal | None:
    """Record a completed turn against the session's open goal."""
    if not session_id:
        return None
    ts = time.time() if now is None else now
    _append_event(
        {
            "event": "turn",
            "session_id": session_id,
            "made_progress": bool(made_progress),
            "ts": ts,
        },
        path=path,
    )
    return current_goal(session_id, path=path)


def complete_goal(
    session_id: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> ThreadGoal | None:
    """Mark the session's goal complete (Sophia declares the task done)."""
    if not session_id:
        return None
    ts = time.time() if now is None else now
    _append_event({"event": "complete", "session_id": session_id, "ts": ts}, path=path)
    return current_goal(session_id, path=path)


def should_continue(
    goal: ThreadGoal | None,
    *,
    made_progress: bool,
    pending_text: str = "",
    max_turns: int = DEFAULT_MAX_GOAL_TURNS,
    stall_seconds: float = 0.0,
    prev_progress_ts: float | None = None,
    now: float | None = None,
) -> GoalDecision:
    """Decide whether the loop may take another turn for this goal.

    Fails closed -- any uncertainty (no goal, no session key) returns ``stop``.
    Order: no-goal -> completion -> always-stop -> ceiling -> stall -> continue.

    ``stall_seconds`` (A3): when > 0, force-stop if the goal has made no progress
    for that many wall-clock seconds. 0 (default) disables the check, so behavior
    is unchanged until a governor opts in.

    ``prev_progress_ts`` (A3): the last-progress timestamp to measure the stall
    from. The wired caller records *this* turn (which, for a progress turn, bumps
    ``goal.last_progress_ts`` to now) **before** calling here -- so measuring from
    ``goal.last_progress_ts`` would always see ~0 idle and never fire. Passing the
    PRE-turn snapshot makes the check mean "time since the last turn that actually
    made progress", which is the useful guard. ``None`` falls back to the goal's
    own ``last_progress_ts`` (pure-primitive callers).
    """
    if goal is None:
        return GoalDecision("stop", "no open goal for this thread")
    if not goal.session_id:
        return GoalDecision("stop", "goal has no session_id (fail-closed)")
    if goal.status != GOAL_OPEN:
        return GoalDecision("done", None)

    always = _always_stop_reason(pending_text)
    if always:
        return GoalDecision("stop", f"always-stop: {always}")

    if max_turns and goal.turns >= max_turns:
        return GoalDecision("stop", f"turn ceiling reached ({goal.turns}/{max_turns})")

    stall_ref_ts = (
        goal.last_progress_ts if prev_progress_ts is None else prev_progress_ts
    )
    if stall_seconds and stall_seconds > 0 and stall_ref_ts > 0:
        ref = time.time() if now is None else now
        idle = ref - stall_ref_ts
        if idle > stall_seconds:
            return GoalDecision(
                "stop",
                f"stall: no progress for {int(idle)}s (limit {int(stall_seconds)}s)",
            )

    if not made_progress:
        return GoalDecision("stop", "no progress this turn (stall)")

    return GoalDecision("continue", None)
