"""Per-turn convergence backstop — graceful wind-down before the round cap.

The chat turn loop (``app/main.py:_run_chat_turn``) runs up to
``CHAT_MAX_TOOL_ROUNDS`` tool rounds. Before this module, a turn that needed
more rounds than the cap ground all the way to the hard limit, got
force-completed with ``tools=None``, leaked DSML tool-call syntax as content,
and surfaced the ``_EMPTY_TURN_FALLBACK`` banner — silent and non-resumable.
A turn could also merge/open a PR and then roll straight into the next plan
unit's discovery, blowing the budget (the 2026-06-28 round-cap incident).

This module makes the *decision* — purely, so it is unit-testable — about when
to inject a one-time convergence directive into the chat history so the model
lands a clean, resumable answer *inside* the budget instead of crashing into
the cap. The caller (main.py) owns the side effects (appending to history,
emitting SSE).

Two triggers:
  * ``pr_boundary`` (#3): a PR open/merge tool already fired this turn — one PR
    per turn, so stop and report; the next unit runs in a fresh turn.
  * ``soft_budget`` (#2): the turn has reached the soft round budget
    (``ceil(max_rounds * fraction)``) and is approaching the hard cap.

See ``agentic_ai_context/ROUND_CAP_RESILIENCE_PLAN.md``.
"""

from __future__ import annotations

import math

# Tools whose firing means a PR boundary was crossed this turn. Mirrors the
# PR-relevant subset of main.py's _SIDE_EFFECT_TOOLS.
_PR_SIDE_EFFECT_TOOLS = {"open_pr", "open_fix_pr", "merge_pr"}


def soft_budget(max_rounds: int, fraction: float) -> int:
    """Round number at/after which the soft backstop fires.

    ``ceil(max_rounds * fraction)``, clamped to ``[1, max_rounds - 1]`` so it
    always lands at least one round *before* the hard cap (leaving room for the
    model to converge) and never on round 0. Degenerate caps (``max_rounds <= 1``)
    fire on round 1.
    """
    if max_rounds <= 1:
        return 1
    raw = math.ceil(max_rounds * fraction)
    return max(1, min(raw, max_rounds - 1))


def should_converge(
    round_num: int,
    max_rounds: int,
    tool_trace: list[dict] | None,
    *,
    soft_fraction: float,
) -> str | None:
    """Return a short reason string if the turn should wind down now, else None.

    First match wins, with ``pr_boundary`` taking precedence over ``soft_budget``
    (a completed PR is a hard turn boundary regardless of how many rounds are left).

    Args:
        round_num: the round just completed (1-based).
        max_rounds: the hard per-turn cap (``CHAT_MAX_TOOL_ROUNDS``).
        tool_trace: the turn's accumulated ``[{"name", "args", "result"}, ...]``.
        soft_fraction: fraction of ``max_rounds`` at which the soft backstop fires.
    """
    for t in tool_trace or []:
        if t.get("name") in _PR_SIDE_EFFECT_TOOLS:
            return "pr_boundary"
    if round_num >= soft_budget(max_rounds, soft_fraction):
        return "soft_budget"
    return None


def convergence_message(reason: str, round_num: int, max_rounds: int) -> dict:
    """The one-time directive appended to history to make the model converge.

    Uses ``role: "user"`` to match the proven mid-loop interjection pattern in
    ``_run_chat_turn`` (a system message mid-conversation is less reliably
    honored by DeepSeek). The directive tells the model to stop calling tools
    and write a clean, resumable final answer.
    """
    if reason == "pr_boundary":
        body = (
            "[TURN DIRECTIVE] You have opened or merged a PR this turn. Per the "
            "one-PR-per-turn rule, STOP here — do NOT begin the next plan unit "
            "(it runs in a fresh turn). Stop calling tools now and write your "
            "final 'what I did this turn' report: the PR link(s), what changed, "
            "and a 'RESUME HERE → <next unit>' pointer. Start no new multi-step work."
        )
    else:  # soft_budget (default)
        body = (
            f"[TURN DIRECTIVE] You have used {round_num} of {max_rounds} tool "
            "rounds and are approaching the per-turn limit. Stop calling tools "
            "now and converge: summarize what you found, what (if anything) is "
            "still blocking, and end with a 'RESUME HERE' pointer so the next "
            "turn can continue. Start no new multi-step work — land a clean, "
            "resumable answer in your next message."
        )
    return {"role": "user", "content": body}
