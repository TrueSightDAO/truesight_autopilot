#!/usr/bin/env python3
"""U4 runner -- one alert pass over the statuses the loop daemon published.

Deliberately a *separate* process (and systemd unit) from ``loop_daemon``: the
reconciler must keep measuring even when Telegram or Discord is unreachable.

Exit codes:
  0  pass completed (alerts may or may not have been sent -- see output)
  1  something went wrong acquiring Redis or delivering an alert
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import loop_alerts  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loop alerts (U4) -- nudge dead threads"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be posted; sends nothing and consumes no dedupe claim",
    )
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="include log-only states (stalled) as if enabled",
    )
    parser.add_argument("--max", type=int, default=loop_alerts.MAX_ALERTS_PER_SWEEP)
    parser.add_argument("--dedupe-s", type=int, default=loop_alerts.DEFAULT_DEDUPE_S)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    environ = None
    if args.all_states:
        import os

        environ = dict(os.environ)
        environ["LOOP_ALERTS_STALLED"] = "true"
        environ["LOOP_ALERTS_LOST_GOAL"] = "true"

    from app.redis_claim import get_client

    try:
        client = get_client()
        client.ping()
    except Exception as exc:
        print(f"loop_alert: redis unavailable: {exc}", file=sys.stderr)
        return 1

    statuses = loop_alerts.read_statuses(client)
    run = loop_alerts.plan_alerts(
        statuses,
        client,
        environ=environ,
        dedupe_s=args.dedupe_s,
        dry_run=args.dry_run,
        max_alerts=args.max,
    )
    run = loop_alerts.deliver_all(
        run, dry_run=args.dry_run, environ=environ, client=client
    )

    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "read": len(statuses),
                    "planned": len(run.planned),
                    "sent": len(run.sent),
                    "skipped_dedupe": len(run.skipped_dedupe),
                    "unmapped": len(run.unmapped),
                    "fallback": len(run.fallback),
                    "capped": run.capped,
                    "errors": run.errors,
                    "targets": [
                        {
                            "session": a.session_id,
                            "state": a.state,
                            "to": a.target.describe(),
                        }
                        for a in run.planned
                    ],
                },
                indent=2,
            )
        )
        return 0 if not run.errors else 1

    verb = "would post" if args.dry_run else "posted"
    print(
        f"loop_alert: read={len(statuses)} planned={len(run.planned)} "
        f"{verb}={len(run.sent)} deduped={len(run.skipped_dedupe)} "
        f"unmapped={len(run.unmapped)} fallback={len(run.fallback)} "
        f"capped={run.capped} errors={len(run.errors)}"
    )
    for alert in run.planned:
        print(f"  [{alert.state}] {alert.target.describe()}  {alert.session_id[-32:]}")
    if run.fallback:
        print("  no channel of their own -> routed to fallback:")
        for f in run.fallback[:10]:
            print(f"    {f}")
    if run.unmapped:
        print("  no channel to post into (reporting, not dropping):")
        for u in run.unmapped[:10]:
            print(f"    {u}")
    for err in run.errors:
        print(f"  ERROR {err}", file=sys.stderr)
    return 0 if not run.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
