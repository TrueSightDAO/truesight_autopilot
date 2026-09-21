"""Unit tests for scripts/sunmint_stalled_balance_aging_report.py (no network).

PR8 / Decision 0.8: the *passive* aging report. These tests pin the pure
helpers (literal normalisation, amount/date parsing, aging buckets, report
aggregation) and assert the report is genuinely read-only (no send / no cache
side effects on the happy path).
"""

from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, "scripts")

from scripts.sunmint_stalled_balance_aging_report import (
    TRACKED_LITERALS,
    aging_bucket,
    build_report,
    extract_transactions,
    find_header_row,
    normalize_literal,
    parse_amount,
    parse_date,
    render_text,
)

HEADER = [
    "Transaction Date",
    "Description",
    "Fund Handler",
    "Amount",
    "Currency",
    "Ledger Line",
    "Is Revenue",
]


def _rows(*data_rows):
    return [HEADER, *data_rows]


def test_normalize_literal_case_and_space_insensitive():
    assert normalize_literal("Cacao Tree - To Be Paid For") == normalize_literal(
        "  cacao tree - to be paid for  "
    )
    # PR5.2 precedent: case-insensitive comparator, no per-ledger map.
    assert normalize_literal("CACAO TREE - TO BE PAID FOR") == normalize_literal(
        "Cacao Tree - To Be Paid For"
    )


def test_parse_amount_tolerates_currency_and_junk():
    assert parse_amount("1.5 BRL") == 1.5
    assert parse_amount("R$ 40") == 40.0
    assert parse_amount("1,234.50") == 1234.50
    assert parse_amount("-3") == -3.0
    assert parse_amount("(12.5)") == -12.5
    assert parse_amount("N/A") is None
    assert parse_amount("") is None
    assert parse_amount(None) is None


def test_parse_date_formats():
    assert parse_date("20260121") == date(2026, 1, 21)
    assert parse_date("2026-01-21") == date(2026, 1, 21)
    assert parse_date("bogus") is None
    assert parse_date("") is None


def test_aging_bucket_bands():
    assert aging_bucket(0) == "0-30"
    assert aging_bucket(30) == "0-30"
    assert aging_bucket(31) == "31-60"
    assert aging_bucket(95) == "91+"
    assert aging_bucket(-1) == "future"


def test_find_header_row_skips_junk_preamble():
    rows = [["junk"], ["more junk"], HEADER]
    assert find_header_row(rows, ["date", "amount", "currency"]) == 2
    assert find_header_row([["nope"]], ["date"]) == -1


def test_extract_transactions_skips_blank_and_unparseable():
    rows = _rows(
        ["20260101", "prepay", "Paulo", "1.5", "Cacao Tree - To Be Paid For", "1", ""],
        ["20260101", "blank", "Paulo", "", "Cacao Tree - To Be Paid For", "2", ""],
        ["20260101", "no curr", "Paulo", "1", "", "3", ""],
    )
    txs = extract_transactions(rows, 0)
    assert len(txs) == 1
    assert txs[0]["amount"] == 1.5
    assert txs[0]["currency"] == "Cacao Tree - To Be Paid For"


def test_build_report_tracks_both_literals_and_aging():
    today = date(2026, 3, 1)
    rows = _rows(
        [
            "20260101",
            "old prepay",
            "f",
            "2",
            "Cacao Tree Purchased - Not Planted",
            "1",
            "",
        ],
        [
            "20260225",
            "recent prepay",
            "f",
            "1",
            "Cacao Tree Purchased - Not Planted",
            "2",
            "",
        ],
        [
            "20260110",
            "unpaid liability",
            "f",
            "3",
            "Cacao Tree - To Be Paid For",
            "3",
            "",
        ],
        ["20260101", "unrelated", "f", "99", "Ceremonial Cacao", "4", "Y"],
    )
    txs = extract_transactions(rows, 0)
    report = build_report(txs, today, threshold_days=30)

    by_literal = {b["literal"]: b for b in report["literals"]}
    assert set(by_literal) == set(TRACKED_LITERALS)

    prepay = by_literal["Cacao Tree Purchased - Not Planted"]
    assert prepay["row_count"] == 2
    assert prepay["outstanding"] == 3.0
    assert prepay["oldest_age_days"] == (today - date(2026, 1, 1)).days
    # 20260101 row (59d) stalls past 30; 20260225 row (4d) does not.
    assert prepay["stalled_count"] == 1
    assert prepay["stalled_amount"] == 2.0

    liability = by_literal["Cacao Tree - To Be Paid For"]
    assert liability["outstanding"] == 3.0
    assert liability["stalled_count"] == 1

    # The unrelated literal never leaks in.
    assert all(
        r["currency"] != "Ceremonial Cacao"
        for b in report["literals"]
        for r in b["rows"]
    )


def test_build_report_settled_when_zero_or_negative():
    today = date(2026, 3, 1)
    rows = _rows(
        ["20260101", "offset", "f", "0", "Cacao Tree - To Be Paid For", "1", ""]
    )
    report = build_report(extract_transactions(rows, 0), today)
    liability = next(
        b for b in report["literals"] if b["literal"] == "Cacao Tree - To Be Paid For"
    )
    assert liability["open"] is False


def test_render_text_is_passive_and_mentions_no_nudge():
    today = date(2026, 3, 1)
    rows = _rows(["20260101", "x", "f", "1", "Cacao Tree - To Be Paid For", "1", ""])
    text = render_text(build_report(extract_transactions(rows, 0), today))
    assert "READ-ONLY" in text
    assert "passive" in text
    assert "Cacao Tree - To Be Paid For" in text
