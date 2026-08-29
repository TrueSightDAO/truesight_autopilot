#!/usr/bin/env python3
"""E2E test: [ASSET RECEIPT EVENT] ingest + mandatory self-clean (expense-off).

Guards the Asset Receipt ingest pipeline (GAS webapp
1o2lzpdTZBYTTFdXzWJoATxznbqL959b_O7_no2Gd-OV4ryOPZOsqxtpU; ops spreadsheet
Telegram Chat Logs -> 'Asset Receipts' audit tab; main ledger 'offchain
transactions' + 'Currencies') against regressions:

  - a (Test ...) currency MUST NOT create a Currencies rate row (QA guard,
    tokenomics #436/#437 -- 2026-08-27: $100/unit x qty 100 = ~$10k phantom
    treasury inflation on truesight.me)
  - the ingest must still write the positive offchain leg (so ingest QA can
    verify end-to-end)
  - the test MUST self-clean afterwards (expense-off): delete the offchain leg
    it created so no phantom inventory remains in the live ledger

Convention: conventions/QA_LIVE_LEDGER_TEST_PROCEDURE.md (agentic_ai_context).

Usage:
  python3 scripts/e2e_asset_receipt_test.py

Exit code 0 = PASS (guard held + self-clean verified), 1 = FAIL.
"""

import datetime
import os
import sys
import time
import urllib.request

import gspread
from google.oauth2 import service_account
from truesight_dao_client.edgar_client import EdgarClient

OPS_SPREADSHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
MAIN_SPREADSHEET_ID = "1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU"
AUDIT_SHEET_NAME = "Asset Receipts"
CURRENCIES_SHEET_NAME = "Currencies"
OFFCHAIN_SHEET_NAME = "offchain transactions"
WEBHOOK_BASE = (
    "https://script.google.com/macros/s/"
    "AKfycbzcXBXYKmKiYg-tS2cqf60gWVm0ro17ndWVMnxNkc0dimaGUW3CYoi4b8nMZzVbENaw"
)
PROCESS_URL = WEBHOOK_BASE + "?action=processAssetReceiptsFromTelegramChatLogs"
CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/opt/truesight_autopilot/config/google/cypher_defense_gdrive_key.json",
)
POLL_SECONDS = 15
POLL_ATTEMPTS = 12


def _client() -> gspread.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def fire_webhook(url: str) -> str:
    """GET the GAS webhook action; GAS redirects, so follow to the echo body."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8", "replace")


def find_audit_row(marker: str):
    """Find the ingest's audit-tab row for this marker (ops spreadsheet)."""
    ws = _client().open_by_key(OPS_SPREADSHEET_ID).worksheet(AUDIT_SHEET_NAME)
    for row in ws.get_all_values():
        if marker in " | ".join(str(c) for c in row[:7]):
            return row
    return None


def find_currency_row(marker: str):
    """Return the Currencies row (main ledger) for this marker, if any."""
    ws = _client().open_by_key(MAIN_SPREADSHEET_ID).worksheet(CURRENCIES_SHEET_NAME)
    for row in ws.get_all_values():
        if marker in str(row[0] or ""):
            return row
    return None


def delete_offchain_legs(marker: str) -> int:
    """Delete offchain-transactions rows whose col E (Currency) == marker.

    This is the expense-off cleanup: removes the positive inventory leg the
    test created. Scans bottom-up so row numbers stay valid.
    """
    ws = _client().open_by_key(MAIN_SPREADSHEET_ID).worksheet(OFFCHAIN_SHEET_NAME)
    all_rows = ws.get_all_values()
    deleted = 0
    for i in range(len(all_rows), 0, -1):
        row = all_rows[i - 1]
        if len(row) > 4 and marker == str(row[4] or "").strip():
            ws.delete_rows(i)
            deleted += 1
    return deleted


def main() -> int:
    marker = "E2E QA Asset (Test " + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S") + ")"
    attrs = {
        "Currency": marker,
        "Amount": "1",
        "Description": "E2E asset receipt test item - self-cleans, no real inventory",
        "Fund Handler": "Sophia Truesight",
        "Destination Contribution File Location": "offchain transactions (E2E test)",
    }
    print(f"[1/6] Submitting [ASSET RECEIPT EVENT] marker={marker}")
    client = EdgarClient.from_env()
    resp = client.submit("ASSET RECEIPT EVENT", attrs)
    body = getattr(resp, "text", "")
    print(f"      HTTP {getattr(resp, 'status_code', resp)} {body[:200]}")
    if '"signature_verification": "success"' not in body:
        print("FAIL: signature verification did not succeed")
        return 1

    print("[2/6] Firing ingest webhook (Telegram Chat Logs -> audit + offchain)")
    try:
        print(fire_webhook(PROCESS_URL)[:200])
    except Exception as exc:  # GAS may exceed the client read window; row still lands
        print(f"      (webhook read timed out: {exc})")

    print("[3/6] Polling Asset Receipts audit tab for marker row")
    row = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        time.sleep(POLL_SECONDS)
        row = find_audit_row(marker)
        if row:
            break
        print(f"      attempt {attempt}/{POLL_ATTEMPTS} ...")
    if not row:
        print("FAIL: marker row never appeared in Asset Receipts audit tab")
        return 1
    status = row[6] if len(row) > 6 else ""
    print(f"      updateId={row[0]} status={status!r}")
    if status.strip().upper() != "OK":
        print(f"FAIL: expected audit status OK, got {status!r}")
        return 1

    print("[4/6] Verifying QA guard: no Currencies rate row created")
    if find_currency_row(marker):
        print("FAIL: QA guard broken - a Currencies rate row was created for a (Test ...) currency")
        return 1
    print("      PASS: no Currencies rate row (guard held)")

    print("[5/6] Self-clean (expense-off): deleting offchain leg(s) for marker")
    deleted = delete_offchain_legs(marker)
    print(f"      deleted {deleted} offchain row(s)")
    if deleted != 1:
        print(f"FAIL: expected exactly 1 offchain leg to clean up, deleted {deleted}")
        return 1

    print("[6/6] Post-cleanup verification")
    if find_currency_row(marker):
        print("FAIL: Currencies row appeared after cleanup")
        return 1
    leftover = delete_offchain_legs(marker)
    if leftover != 0:
        print(f"FAIL: {leftover} offchain leg(s) still present after cleanup")
        return 1
    print("PASS: E2E asset receipt processed, guard held, ledger fully self-cleaned")
    print("NOTE: the audit-tab row is intentionally kept (dedup guard prevents re-processing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
