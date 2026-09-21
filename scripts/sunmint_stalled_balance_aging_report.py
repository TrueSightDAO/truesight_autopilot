"""Aging report for stalled SunMint tree balances (plan PR8; Decision 0.8).

READ-ONLY. This is the *passive report* form of Decision 0.8: it surfaces
balances that have been sitting unresolved. It does NOT send outreach, write
ledger rows, or notify anyone -- it only reads the main ledger's
``offchain transactions`` tab and prints (or serialises) an aging summary.

Two literals are tracked (see ``tokenomics/SCHEMA.md`` -- Tree-Planting
Ledger Literals):

* ``Cacao Tree Purchased - Not Planted`` -- a prepayment. The DAO paid a farmer
  for a tree not yet confirmed planted; a lingering positive balance means the
  prepayment has not been matched by a planting confirmation.
* ``Cacao Tree - To Be Paid For`` -- a liability. A planting was confirmed
  before payment; a lingering positive balance means an accrued, still-unpaid
  obligation to a farmer.

Both literals live on the MAIN ledger only (SCHEMA.md; plan Decisions 0.9 and
0.12), so a single read of the main ``offchain transactions`` tab is
authoritative -- managed ledgers are intentionally out of scope.

Usage:
    python3 scripts/sunmint_stalled_balance_aging_report.py
    python3 scripts/sunmint_stalled_balance_aging_report.py --threshold-days 45
    python3 scripts/sunmint_stalled_balance_aging_report.py --json

Set ``GOOGLE_APPLICATION_CREDENTIALS`` to a service-account JSON that can read
the main ledger. The report is read-only; the exit code is 0 for a report (even
with stalled balances) and non-zero only on a configuration / I-O error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

# --- Config ---------------------------------------------------------------

# Main ledger (tokenomics). Both tracked literals are main-only.
MAIN_LEDGER_ID = "1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU"
OFFCHAIN_TAB = "offchain transactions"

TRACKED_LITERALS = (
    "Cacao Tree Purchased - Not Planted",
    "Cacao Tree - To Be Paid For",
)

# `offchain transactions` columns (0-based; see tokenomics/SCHEMA.md).
OFFCHAIN_COLS = {
    "date": 0,  # A Transaction Date
    "description": 1,  # B Description
    "fund_handler": 2,  # C Fund Handler
    "amount": 3,  # D Amount
    "currency": 4,  # E Currency
    "ledger_line": 5,  # F Ledger Line
    "is_revenue": 6,  # G Is Revenue
}

DEFAULT_THRESHOLD_DAYS = 30
AGING_EDGES = (30, 60, 90)

_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y")


# --- Pure helpers (unit-tested, no I/O) -----------------------------------


def normalize_literal(value: str) -> str:
    """Case-insensitive, whitespace-collapsed key for a currency literal."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def parse_amount(value) -> float | None:
    """Parse a sheet amount cell; tolerate ``R$``/``$``/commas/parens.

    Returns ``None`` for blank / unparseable values (e.g. ``"N/A"``) so the
    caller can skip the row rather than book a phantom zero.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        amount = float(s)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value: str) -> date | None:
    """Parse a sheet date cell across the formats seen on the ledger."""
    s = (value or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def aging_bucket(days: int, edges: tuple[int, ...] = AGING_EDGES) -> str:
    """Bucket an age in whole days into a coarse aging band."""
    if days < 0:
        return "future"
    lower = 0
    for edge in edges:
        if days <= edge:
            return f"{lower}-{edge}"
        lower = edge + 1
    return f"{lower}+"


def find_header_row(rows, required) -> int:
    """Return the 0-based index of the row whose text carries all keywords."""
    for i, row in enumerate(rows):
        text = " ".join(str(c).lower() for c in row)
        if all(token.lower() in text for token in required):
            return i
    return -1


def _cell(row, idx):
    return row[idx] if idx < len(row) else ""


def extract_transactions(rows, header_row):
    """Turn raw sheet rows into typed transaction dicts (skips junk rows)."""
    out = []
    for row in rows[header_row + 1 :]:
        currency = _cell(row, OFFCHAIN_COLS["currency"]).strip()
        amount = parse_amount(_cell(row, OFFCHAIN_COLS["amount"]))
        if not currency or amount is None:
            continue
        out.append(
            {
                "date": _cell(row, OFFCHAIN_COLS["date"]).strip(),
                "description": _cell(row, OFFCHAIN_COLS["description"]).strip(),
                "fund_handler": _cell(row, OFFCHAIN_COLS["fund_handler"]).strip(),
                "amount": amount,
                "currency": currency,
                "is_revenue": _cell(row, OFFCHAIN_COLS["is_revenue"]).strip(),
            }
        )
    return out


def build_report(transactions, today, threshold_days=DEFAULT_THRESHOLD_DAYS):
    """Aggregate tracked-literal rows into a per-literal aging summary."""
    blocks = []
    for literal in TRACKED_LITERALS:
        key = normalize_literal(literal)
        rows = []
        for tx in transactions:
            if normalize_literal(tx["currency"]) != key:
                continue
            when = parse_date(tx["date"])
            age = (today - when).days if when else None
            rows.append(
                {
                    **tx,
                    "age_days": age,
                    "bucket": aging_bucket(age) if age is not None else "unknown",
                }
            )
        outstanding = round(sum(r["amount"] for r in rows), 2)
        ages = [r["age_days"] for r in rows if r["age_days"] is not None]
        stalled = [
            r
            for r in rows
            if r["age_days"] is not None and r["age_days"] > threshold_days
        ]
        bucket_totals: dict[str, float] = {}
        for r in rows:
            bucket_totals[r["bucket"]] = round(
                bucket_totals.get(r["bucket"], 0.0) + r["amount"], 2
            )
        blocks.append(
            {
                "literal": literal,
                "row_count": len(rows),
                "outstanding": outstanding,
                "open": outstanding > 0,
                "oldest_age_days": max(ages) if ages else None,
                "stalled_count": len(stalled),
                "stalled_amount": round(sum(r["amount"] for r in stalled), 2),
                "bucket_totals": bucket_totals,
                "rows": rows,
            }
        )
    return {
        "generated_for": today.isoformat(),
        "threshold_days": threshold_days,
        "literals": blocks,
    }


def render_text(report) -> str:
    lines = [
        "SunMint stalled-balance aging report (READ-ONLY, passive -- no outreach)",
        (
            f"As of {report['generated_for']} | "
            f"stalled threshold {report['threshold_days']}d"
        ),
        "",
    ]
    for block in report["literals"]:
        state = "OPEN" if block["open"] else "settled"
        lines.append(f"* {block['literal']}  [{state}]")
        lines.append(
            f"    rows={block['row_count']}  "
            f"outstanding={block['outstanding']:g}  "
            f"oldest={block['oldest_age_days']}d  "
            f"stalled={block['stalled_count']} "
            f"({block['stalled_amount']:g})"
        )
        for bucket, total in sorted(block["bucket_totals"].items()):
            lines.append(f"      bucket {bucket:>8}: {total:g}")
        lines.append("")
    lines.append(
        "Note: this is a report only. Decision 0.8 = passive; no nudging is sent."
    )
    return "\n".join(lines)


# --- I/O ------------------------------------------------------------------


def fetch_offchain_rows(spreadsheet_id: str, credentials_path: str):
    """Read the main ledger offchain tab (lazy gspread import for tests)."""
    import gspread  # lazy: keeps hermetic test imports working without gspread

    gc = gspread.service_account(filename=credentials_path)
    sh = gc.open_by_key(spreadsheet_id)
    return sh.worksheet(OFFCHAIN_TAB).get_all_values()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", default=MAIN_LEDGER_ID)
    parser.add_argument("--threshold-days", type=int, default=DEFAULT_THRESHOLD_DAYS)
    parser.add_argument("--json", action="store_true", help="emit JSON, not text")
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    )
    args = parser.parse_args(argv)

    if not args.credentials or not os.path.isfile(args.credentials):
        print(
            "ERROR: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON",
            file=sys.stderr,
        )
        return 2
    try:
        rows = fetch_offchain_rows(args.spreadsheet_id, args.credentials)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to read offchain tab: {exc}", file=sys.stderr)
        return 1

    header_row = find_header_row(rows, ["date", "description", "amount", "currency"])
    if header_row < 0:
        print("ERROR: could not locate the offchain header row", file=sys.stderr)
        return 1

    transactions = extract_transactions(rows, header_row)
    report = build_report(
        transactions, datetime.now(timezone.utc).date(), args.threshold_days
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
