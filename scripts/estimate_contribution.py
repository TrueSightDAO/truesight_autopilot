#!/usr/bin/env python3
"""estimate_contribution.py — standard per-seat contribution-time estimator.

Reads a local Sophia session (sessions/<hash>.json and, when present,
sessions/<hash>_debug.log) and estimates contribution minutes for the three
seats, following the raw-vs-direct split convention in
agentic_ai_context/dao/DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md:

    Sophia Truesight — "Raw machine execution"             (separate event)
    Sophia Truesight — "Direct time (engagement/analysis)" (separate event)
    Gary Teh         — "Gary Teh direct time"              (separate event)
    Envoy TrueSight  — no local transcript source yet       (see OPEN_FOLLOWUPS.md)

Calibration anchors (2026-08-24 incident, thread 14165, documented in the same
convention doc): ~200 tool ops ~= 60 min raw execution; ~17 governor messages
~= 60 min Gary direct time. All numbers are informational estimates — the
governor reviews and approves the final values before any submission.

Usage:
    python3 scripts/estimate_contribution.py --session 22f8f538dedd
    python3 scripts/estimate_contribution.py --session 22f8f538dedd --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

SESSION_LOG_DIR = Path(
    os.getenv("SOPHIA_SESSION_DIR", "/opt/truesight_autopilot/sessions")
)
PREFIX_WRAPPER_RE = re.compile(
    r"\[(?:GOVERNOR_IDENTITY|Telegram context|Handoff context)[^\]]*\]"
)
# Harness-injected auto-turns that are NOT governor input (whole turn dropped):
#   [TURN DIRECTIVE ...]  — turn-control / tool-round-limit notices
#   [emoji-go: ...] with quoted resume text or bare go-word — resume replays
TURN_DIRECTIVE_RE = re.compile(r"\[TURN DIRECTIVE[^\]]*\]")
EMOJI_GO_RE = re.compile(r"\[emoji-go[^\]]*\]")
GOVERNOR_ID_RE = re.compile(r"GOVERNOR_IDENTITY:\s*You are speaking with\s+([^.\]]+)\.")
LLM_ROUND_RE = re.compile(r"=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) llm-round-")
# Auto-ping phrases (reaction-driven, not substantive governor input).
AUTO_PING_WORDS = {
    "go",
    "go for it",
    "resume",
    "resume this",
    "proceed",
    "do it",
    "ok",
    "okay",
    "yes",
    "yeah",
    "\U0001f44d",
    "\U0001f44d\ufe0f",
    "\u2705",
    "\ud83d\udc4d",
}

DEFAULTS = {
    "tool_op_sec": 18.0,  # 200 ops ~= 60 min raw (2026-08-24 anchor)
    "per_gov_msg_min": 3.5,  # 17 msgs  ~= 60 min Gary direct (anchor)
    "per_llm_round_min": 1.5,  # human-equivalent analysis per LLM round
    "gap_trim_min": 5.0,  # idle gaps longer than this are not machine time
}


def _load_session(session_hash: str) -> dict:
    p = SESSION_LOG_DIR / f"{session_hash}.json"
    if not p.exists():
        raise SystemExit(f"session file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _llm_round_timestamps(session_hash: str) -> list[datetime]:
    log = SESSION_LOG_DIR / f"{session_hash}_debug.log"
    ts: list[datetime] = []
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = LLM_ROUND_RE.match(line.strip())
            if m:
                ts.append(datetime.fromisoformat(m.group(1)))
    return ts


def _fmt_span(ts: list[datetime]) -> str:
    if len(ts) < 2:
        return ""
    return f"{ts[0]:%H:%M:%S}-{ts[-1]:%H:%M:%S}"


def _active_minutes(ts: list[datetime], gap_trim_min: float) -> float:
    """Wall-clock span with idle gaps (> gap_trim_min) removed."""
    if len(ts) < 2:
        return 0.0
    total = 0.0
    for prev, cur in zip(ts, ts[1:]):
        gap = (cur - prev).total_seconds() / 60.0
        total += min(gap, gap_trim_min)
    return round(total, 1)


def _count_role(history: list[dict], role: str) -> int:
    return sum(1 for m in history if m.get("role") == role)


def _substantive_governor_msgs(history: list[dict]) -> list[str]:
    out: list[str] = []
    for m in history:
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "").strip()
        # Whole-turn droppers: harness-injected auto-turns are NOT governor
        # input — [TURN DIRECTIVE] turn-control notices, and resume replays
        # that quote a prior transcript ("original resume text: ...").
        if TURN_DIRECTIVE_RE.search(text) or "original resume text:" in text.lower():
            continue
        # Strip every known wrapper tag (informational prefixes + emoji-go).
        text = PREFIX_WRAPPER_RE.sub("", text)
        text = EMOJI_GO_RE.sub("", text).strip()
        # Drop bare auto-ping reactions / go-words (not fresh governor input)
        # and anything too short to be substantive.
        if not text:
            continue
        lowered = text.lower().rstrip(" .!")
        if lowered in AUTO_PING_WORDS:
            continue
        if len(text) >= 10:
            out.append(text)
    return out


def _governor_name(history: list[dict]) -> str:
    for m in history:
        text = str(m.get("content") or "")
        match = GOVERNOR_ID_RE.search(text)
        if match:
            return match.group(1).strip()
    return "Gary Teh"


def estimate(session_hash: str, overrides: dict) -> dict:
    session = _load_session(session_hash)
    history = session.get("full_history", [])
    rounds_ts = _llm_round_timestamps(session_hash)
    tool_ops = _count_role(history, "tool")
    assistant_msgs = _count_role(history, "assistant")
    gov_msgs = _substantive_governor_msgs(history)
    llm_rounds = len(rounds_ts) or assistant_msgs

    raw_min = _active_minutes(rounds_ts, overrides["gap_trim_min"])
    if not rounds_ts:
        raw_min = round(tool_ops * overrides["tool_op_sec"] / 60.0, 1)

    return {
        "session": session_hash,
        "governor": _governor_name(history),
        "history_msgs": len(history),
        "tool_ops": tool_ops,
        "llm_rounds": llm_rounds,
        "gov_messages": len(gov_msgs),
        "span": _fmt_span(rounds_ts),
        "raw_min": raw_min,
        "direct_min": round(llm_rounds * overrides["per_llm_round_min"], 1),
        "gary_min": round(len(gov_msgs) * overrides["per_gov_msg_min"], 1),
        "envoy_min": float(overrides.get("envoy_minutes", 0.0)),
    }


def _emit_table(est: dict, span: str) -> None:
    rows = [
        (
            "Sophia Truesight",
            "Raw machine execution",
            est["raw_min"],
            f"wall-clock {span} across {est['llm_rounds']} llm rounds "
            "(idle gaps >5.0m trimmed)"
            if span
            else f"tool ops x 18s ({est['tool_ops']} ops)",
        ),
        (
            "Sophia Truesight",
            "Direct time (engagement/analysis)",
            est["direct_min"],
            f"{est['llm_rounds']} llm rounds x 1.5 min",
        ),
        (
            "Gary Teh",
            "Gary Teh direct time",
            est["gary_min"],
            f"{est['gov_messages']} governor msgs x 3.5 min",
        ),
        (
            "Envoy TrueSight",
            "(no local source)",
            est["envoy_min"],
            "see OPEN_FOLLOWUPS.md entry",
        ),
    ]
    print(f"session      : {est['session']}")
    print(f"governor     : {est['governor']}")
    print(
        f"history msgs : {est['history_msgs']} (tool ops {est['tool_ops']}, "
        f"llm rounds {est['llm_rounds']}, gov msgs {est['gov_messages']})"
    )
    print()
    print(f"{'seat':<20} {'event':<38} {'min':>7}  basis")
    print("-" * 100)
    for seat, event, mins, basis in rows:
        print(f"{seat:<20} {event:<38} {mins:>7}  {basis}")


def _emit_payloads(est: dict) -> None:
    print()
    print(
        "Suggested [CONTRIBUTION EVENT] payloads (informational \u2014 governor approves):"
    )
    payloads = [
        ("Raw machine execution", est["raw_min"]),
        ("Direct time (engagement/analysis)", est["direct_min"]),
    ]
    # Gary is a separate seat/event only when he has substantive messages.
    if est["gov_messages"]:
        payloads.append(("Gary Teh direct time", est["gary_min"]))
    if est["envoy_min"] > 0:
        payloads.append(("Envoy TrueSight direct time", est["envoy_min"]))
    for desc, mins in payloads:
        print(
            json.dumps(
                {
                    "Type": "Time (Minutes)",
                    "Amount": str(mins),
                    "Contributors": "Gary Teh"
                    if desc == "Gary Teh direct time"
                    else "Envoy TrueSight"
                    if "Envoy" in desc
                    else "Sophia Truesight",
                    "Description": f"{desc} \u2014 session {est['session']}",
                },
                indent=4,
                ensure_ascii=False,
            )
        )
        print()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="session hash, e.g. 22f8f538dedd")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of table")
    ap.add_argument(
        "--envoy-minutes",
        type=float,
        default=0.0,
        help="Envoy minutes override (no local transcript source yet)",
    )
    args = ap.parse_args(argv)

    overrides = {
        "tool_op_sec": DEFAULTS["tool_op_sec"],
        "per_gov_msg_min": DEFAULTS["per_gov_msg_min"],
        "per_llm_round_min": DEFAULTS["per_llm_round_min"],
        "gap_trim_min": DEFAULTS["gap_trim_min"],
        "envoy_minutes": args.envoy_minutes,
    }
    est = estimate(args.session, overrides)
    if args.json:
        print(json.dumps(est, indent=2, ensure_ascii=False))
    else:
        _emit_table(est, est["span"])
        _emit_payloads(est)


if __name__ == "__main__":
    main()
