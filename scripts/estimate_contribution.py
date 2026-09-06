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
    python3 scripts/estimate_contribution.py --session 22f8f538dedd --envoy-minutes 90
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SESSION_LOG_DIR = Path(
    os.getenv("SOPHIA_SESSION_DIR", "/opt/truesight_autopilot/sessions")
)
CONTEXT_WRAPPER_RE = re.compile(
    r"\[(?:GOVERNOR_IDENTITY|Telegram context|Handoff context|emoji-go)[^\]]*\]"
)
GOVERNOR_ID_RE = re.compile(r"GOVERNOR_IDENTITY:\s*You are speaking with\s+([^.\]]+)\.")
LLM_ROUND_RE = re.compile(r"=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) llm-round-")

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
        text = CONTEXT_WRAPPER_RE.sub("", text).strip()
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
    if raw_min:
        raw_basis = (
            f"wall-clock {_fmt_span(rounds_ts)} across {llm_rounds} llm rounds"
            f" (idle gaps >{overrides['gap_trim_min']}m trimmed)"
        )
    else:
        raw_min = round(tool_ops * overrides["tool_op_sec"] / 60.0, 1)
        raw_basis = f"{tool_ops} tool ops x {overrides['tool_op_sec']}s (no debug log)"

    direct_min = round(llm_rounds * overrides["per_llm_round_min"], 1)
    gary_min = round(len(gov_msgs) * overrides["per_gov_msg_min"], 1)

    return {
        "session": session_hash,
        "governor": _governor_name(history),
        "history_msgs": len(history),
        "tool_ops": tool_ops,
        "llm_rounds": llm_rounds,
        "gov_messages": len(gov_msgs),
        "estimates": {
            "sophia_raw_min": raw_min,
            "sophia_direct_min": direct_min,
            "gary_direct_min": gary_min,
        },
        "basis": {
            "sophia_raw": raw_basis,
            "sophia_direct": (
                f"{llm_rounds} llm rounds x {overrides['per_llm_round_min']} min"
            ),
            "gary_direct": (
                f"{len(gov_msgs)} governor msgs x {overrides['per_gov_msg_min']} min"
            ),
        },
    }


def _payload(contributors: str, minutes: float, description: str) -> dict:
    return {
        "Type": "Time (Minutes)",
        "Amount": str(minutes),
        "Contributors": contributors,
        "Description": description,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="12-hex session hash")
    ap.add_argument("--json", action="store_true", help="emit raw JSON only")
    for key, default in DEFAULTS.items():
        ap.add_argument(
            f"--{key}",
            type=float,
            default=default,
            help=f"(default {default})",
        )
    ap.add_argument(
        "--envoy-minutes",
        type=float,
        default=0.0,
        help="Envoy TrueSight minutes, if Gary supplies them from nelanco-claude",
    )
    args = ap.parse_args(argv)
    overrides = {key: getattr(args, key) for key in DEFAULTS}
    try:
        est = estimate(args.session, overrides)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(est, indent=2))
        return 0

    e = est["estimates"]
    print(f"session      : {est['session']}")
    print(f"governor     : {est['governor']}")
    print(
        f"history msgs : {est['history_msgs']} (tool ops {est['tool_ops']},"
        f" llm rounds {est['llm_rounds']}, gov msgs {est['gov_messages']})"
    )
    print()
    print("seat                 event                                      min   basis")
    rows = [
        (
            "Sophia Truesight",
            "Raw machine execution",
            e["sophia_raw_min"],
            est["basis"]["sophia_raw"],
        ),
        (
            "Sophia Truesight",
            "Direct time (engagement/analysis)",
            e["sophia_direct_min"],
            est["basis"]["sophia_direct"],
        ),
        (
            "Gary Teh",
            "Gary Teh direct time",
            e["gary_direct_min"],
            est["basis"]["gary_direct"],
        ),
        (
            "Envoy TrueSight",
            "(no local source)",
            args.envoy_minutes,
            "supplied by Gary from nelanco-claude"
            if args.envoy_minutes
            else "see OPEN_FOLLOWUPS.md entry",
        ),
    ]
    for seat, event, minutes, basis in rows:
        print(f"{seat:<17} {event:<40} {minutes:>5}  {basis}")
    print()
    print(
        "Suggested [CONTRIBUTION EVENT] payloads (informational — governor approves):"
    )
    for seat, event, minutes in [
        ("Sophia Truesight", "Raw machine execution", e["sophia_raw_min"]),
        (
            "Sophia Truesight",
            "Direct time (engagement/analysis)",
            e["sophia_direct_min"],
        ),
        ("Gary Teh", "Gary Teh direct time", e["gary_direct_min"]),
    ]:
        print(
            json.dumps(
                _payload(seat, minutes, f"{event} — session {est['session']}"), indent=4
            )
        )
        print()
    if args.envoy_minutes:
        print(
            json.dumps(
                _payload(
                    "Envoy TrueSight",
                    args.envoy_minutes,
                    f"Envoy direct time — session {est['session']} (interactive, nelanco-claude)",
                ),
                indent=4,
            )
        )
        print()
    print(
        "Submit via dao_client report_ai_agent_contribution.py (PR/commit evidence"
        " required) or the DApp; TDG Issued stays 0 unless the governor awards."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
