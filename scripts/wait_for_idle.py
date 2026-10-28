#!/usr/bin/env python3
"""Refuse to restart the autopilot while chat turns are in flight.

Why this exists
---------------
``scripts/deploy.sh`` used to print "Waiting 5s for active requests to drain"
and then restart unconditionally. Five seconds is not a drain: a chat turn
routinely runs for minutes, so the restart landed mid-turn and severed it.

Observed 2026-10-28: at 14:19:32 systemd logged

    Stopped TrueSight Autopilot -- Autonomous SRE + Developer
    Started TrueSight Autopilot -- Autonomous SRE + Developer

while a turn had a tool call in flight (the last log line before the boundary
was an ``rc=255``). That thread went silent and had to be nudged back by hand.

P0 (``app/turn_heartbeat.py``) made a live turn keep its lease *fresh* for as
long as it is genuinely working, so ``active_tracks.json`` is now a
trustworthy cross-process busy-signal. That is what this gate reads --
``deploy_autopilot``'s in-process ``_active_streams`` check is not visible to a
shell script.

Exit codes
----------
0   no active turns (safe to restart), or ``--force`` was passed
1   turns still active after the drain window -- DO NOT restart
2   could not evaluate the lease file -- caller decides (see ``--on-error``)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
from pathlib import Path
from typing import Any

# Deliberately duplicated from app/loop_status.py rather than imported: this
# gate must still work when the working tree is half-updated or the app package
# has an import problem, which is exactly when a deploy is running. The test
# suite asserts this mirror agrees with loop_status on real fixture data, so
# the two cannot silently drift.
DEFAULT_TRACK_LEASE_S = 120.0
# Mirrors app/loop_status.CHAT_TRACK_TYPES exactly: test_constants_mirror_loop_status
# fails on drift. A missed type => an undetected live turn => a severed deploy.
CHAT_TRACK_TYPES = frozenset(
    {
        "telegram_chat",
        "discord_chat",
        "chat",
        "telegram",
        "discord",
    }
)
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "error"}


def _parse_ts(value: Any) -> float | None:
    """ISO-8601 (``...Z``) or epoch seconds -> epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _session_id(track: dict) -> str:
    for key in ("id", "session_id"):
        val = track.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    meta = track.get("metadata")
    if isinstance(meta, dict):
        val = meta.get("session_id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def active_turns(raw: Any, *, now: float, max_age_s: float | None = None) -> list[dict]:
    """Running chat turns whose lease has not expired.

    ``max_age_s`` additionally ignores entries whose last sign of life is older
    than that, matching ``_other_threads_busy``'s 180s grace for entries left
    behind by a hard kill.
    """
    items = raw.get("tracks") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    busy: list[dict] = []
    for track in items:
        if not isinstance(track, dict):
            continue
        if track.get("track_type") not in CHAT_TRACK_TYPES:
            continue
        status = str(track.get("status") or "").strip().lower()
        if status in TERMINAL_STATUSES:
            continue

        started = _parse_ts(track.get("started_at"))
        beat = _parse_ts(track.get("last_heartbeat"))
        last_alive = beat if beat is not None else started
        if last_alive is None:
            continue

        age = now - last_alive
        if age < 0:
            age = 0.0
        try:
            lease = float(track.get("expected_max_duration_s") or DEFAULT_TRACK_LEASE_S)
        except (TypeError, ValueError):
            lease = DEFAULT_TRACK_LEASE_S
        if age > lease:
            continue  # expired: the turn is gone, not busy
        if max_age_s is not None and age > max_age_s:
            continue

        busy.append(
            {
                "session_id": _session_id(track) or "(unknown)",
                "label": str(track.get("label") or ""),
                "age_s": round(age, 1),
                "lease_s": lease,
            }
        )
    return busy


def _load(path: Path) -> Any:
    # Deliberately does NOT treat a missing file as "idle": a deploy gate that
    # reads unknown state as safe is worse than no gate. main() maps the error
    # to exit 2, or proceeds if the operator passed --on-error=proceed.
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="/opt/truesight_autopilot")
    ap.add_argument(
        "--drain", type=float, default=30.0, help="seconds to wait for turns to finish"
    )
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument(
        "--max-age",
        type=float,
        default=180.0,
        help="ignore entries with no sign of life for longer than this (0 disables)",
    )
    ap.add_argument(
        "--force", action="store_true", help="restart anyway (severs live turns)"
    )
    ap.add_argument(
        "--on-error",
        choices=("abort", "proceed"),
        default="abort",
        help="what to do if the lease file cannot be read",
    )
    args = ap.parse_args(argv)

    path = Path(args.repo) / "data" / "active_tracks.json"
    max_age = args.max_age or None
    deadline = time.time() + max(0.0, args.drain)

    if args.force:
        try:
            raw = _load(path)
            busy = active_turns(raw, now=time.time(), max_age_s=max_age)
        except (OSError, json.JSONDecodeError):
            busy = []
        print(
            "wait_for_idle: --force given, restarting without waiting"
            + (f"; {len(busy)} turn(s) may be severed." if busy else "."),
            file=sys.stderr,
        )
        return 0

    try:
        raw = _load(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"wait_for_idle: cannot read {path}: {exc}", file=sys.stderr)
        return 0 if args.on_error == "proceed" else 2

    while True:
        now = time.time()
        busy = active_turns(raw, now=now, max_age_s=max_age)
        if not busy:
            print("wait_for_idle: no active turns -- safe to restart.")
            return 0
        if not args.force and now >= deadline:
            break
        time.sleep(min(args.interval, max(0.0, deadline - now)) or args.interval)
        try:
            raw = _load(path)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"wait_for_idle: lease unreadable mid-wait: {exc}", file=sys.stderr)
            return 0 if args.on_error == "proceed" else 2

    print(
        f"wait_for_idle: {len(busy)} turn(s) still active after "
        f"{args.drain:g}s -- REFUSING to restart (this would sever them):",
        file=sys.stderr,
    )
    for b in busy:
        print(
            f"  - {b['session_id']}  {b['label']}  idle {b['age_s']}s / lease {b['lease_s']:g}s",
            file=sys.stderr,
        )
    print(
        "  Re-run the deploy once they finish, or pass --force to override.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
