#!/usr/bin/env python3
"""Daily rollup: sum per-contributor LLM usage and submit [CONTRIBUTION EVENT] to Edgar.

Reads usage/<date>/workers.jsonl + all session usage.jsonl for the given date,
groups by provider+contributor, sums est_usd, and submits one contribution per
contributor per day.

Usage:
    python3 scripts/rollup_llm_contributions.py [--date YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Provider → contributor mapping (add entries for each financed provider)
PROVIDER_CONTRIBUTORS: dict[str, str] = {
    "bigmodel": "Elizabeth Wong",
    # "deepseek": "TrueSight DAO",  # DAO-funded, no external contribution to log
}

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SESSION_LOG_DIR = Path(os.getenv("SESSION_LOG_DIR", "/tmp/autopilot_sessions"))


def load_day_usage(date_str: str) -> list[dict]:
    """Load all usage records for a given date from session + worker files."""
    records: list[dict] = []
    date_dir = SESSION_LOG_DIR / "usage" / date_str

    # Worker log
    worker_file = date_dir / "workers.jsonl"
    if worker_file.exists():
        for line in worker_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Session logs
    for session_dir in SESSION_LOG_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        usage_file = session_dir / "usage.jsonl"
        if not usage_file.exists():
            continue
        for line in usage_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    # Filter by date
                    ts = rec.get("ts", "")
                    if ts.startswith(date_str):
                        records.append(rec)
                except json.JSONDecodeError:
                    pass

    return records


def rollup(records: list[dict]) -> dict[str, dict[str, float]]:
    """Group by provider -> contributor, summing est_usd.

    Returns: {provider: {contributor_name: total_usd}}
    """
    by_provider: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for rec in records:
        provider = rec.get("provider", "")
        contributor = PROVIDER_CONTRIBUTORS.get(provider)
        if not contributor:
            continue
        est = rec.get("est_usd")
        if est is None:
            continue
        by_provider[provider][contributor] += float(est)
    return dict(by_provider)


def submit_contribution(contributor: str, amount_usd: float, date_str: str, dry_run: bool = False) -> bool:
    """Submit a [CONTRIBUTION EVENT] to Edgar."""
    payload = {
        "Type": "USD",
        "Amount": f"{amount_usd:.6f}",
        "Description": f"BigModel.cn LLM tokens — autopilot usage {date_str}",
        "Contributors": contributor,
    }
    if dry_run:
        print(f"  [DRY RUN] Would submit for {contributor}: ${amount_usd:.6f}")
        return True

    try:
        from truesight_dao_client.edgar_client import EdgarClient

        from app.config import settings

        client = EdgarClient(
            email=settings.email,
            public_key_b64=settings.public_key,
            private_key_b64=settings.private_key,
            generation_source="https://github.com/TrueSightDAO/truesight_autopilot",
        )
        resp = client.submit("CONTRIBUTION EVENT", payload)
        if resp.ok:
            print(f"  Submitted for {contributor}: ${amount_usd:.6f}")
            return True
        else:
            print(f"  Failed ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  Error: {e}")
        return False


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = argparse.ArgumentParser(description="Roll up LLM usage into DAO contributions")
    p.add_argument("--date", default=today, help=f"Date to roll up (default: {today})")
    p.add_argument("--dry-run", action="store_true", help="Print only, no submissions")
    args = p.parse_args()

    records = load_day_usage(args.date)
    print(f"Loaded {len(records)} usage records for {args.date}")

    grouped = rollup(records)
    if not grouped:
        print("No financed providers with usage today. Nothing to submit.")
        return

    ok_all = True
    for _provider, contributors in grouped.items():
        for name, total in contributors.items():
            ok = submit_contribution(name, total, args.date, args.dry_run)
            if not ok:
                ok_all = False

    if args.dry_run:
        print("\n[Dry run — no submissions made]")
    elif ok_all:
        print("\nAll contributions submitted.")
    else:
        print("\nSome submissions failed — check logs.")
        sys.exit(1)


if __name__ == "__main__":
    main()
