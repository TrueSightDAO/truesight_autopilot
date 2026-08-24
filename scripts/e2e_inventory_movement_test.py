#!/usr/bin/env python3
"""E2E test: self-to-self inventory movement must reach PROCESSED (col N).

Guards the Inventory Movement auth pipeline (spreadsheet
1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ, tab 'Inventory Movement',
column N = STATUS) against regressions:
  - signer key must resolve to an ACTIVE registered contributor
  - the deployed webhook must be the current version (not a pinned old deploy)
  - fresh submissions (rows already in the sheet are never re-scanned)

Usage:
  python3 scripts/e2e_inventory_movement_test.py

Exit code 0 = PASS (col N == PROCESSED), 1 = FAIL.
"""

import datetime
import os
import sys
import time
import urllib.request

import gspread
from google.oauth2 import service_account
from truesight_dao_client.edgar_client import EdgarClient

INVENTORY_SPREADSHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
INVENTORY_SHEET_NAME = "Inventory Movement"
TELEGRAM_SHEET_NAME = "Telegram Chat Logs"
WEBHOOK_BASE = (
    "https://script.google.com/macros/s/"
    "AKfycbzECOd1Y3mH7L0zU8hOC4AxQctYICX0Ws8j2-Md1dWg0k3GFGQx_4Cf7n-CM0usmSJ1/exec"
)
PHASE_1_URL = WEBHOOK_BASE + "?action=processTelegramChatLogs"
PHASE_2_URL = WEBHOOK_BASE + "?action=processInventoryMovementToLedgers"
CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/opt/truesight_autopilot/config/google/cypher_defense_gdrive_key.json",
)
POLL_SECONDS = 15
POLL_ATTEMPTS = 12


def fire_webhook(url: str) -> str:
    """GET the GAS webhook action; GAS redirects, so follow to the echo body."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8", "replace")


def find_row_by_marker(sheet_name: str, marker: str):
    creds = service_account.Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(INVENTORY_SPREADSHEET_ID)
    ws = sh.worksheet(sheet_name)
    for row in ws.get_all_values():
        if marker in " | ".join(str(c) for c in row[:8]):
            return row
    return None


def main() -> int:
    marker = "e2e-self-to-self-" + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    attrs = {
        "Manager Name": "Sophia Truesight",
        "Recipient Name": "Sophia Truesight",
        "Inventory Item": "E2E TEST ITEM - Cacao Tea 50g QR label (self-to-self, no real inventory)",
        "Quantity": "1",
        "Destination Inventory File Location": (
            "Agroverse QR codes sheet (Cacao Tea 50g batch, manager column)"
        ),
        "Approved By": "Gary Teh",
        "Submission Source": marker,
    }
    print(f"[1/5] Submitting self-to-self movement marker={marker}")
    client = EdgarClient.from_env()
    resp = client.submit("INVENTORY MOVEMENT", attrs)
    body = getattr(resp, "text", "")
    print(f"      HTTP {getattr(resp, 'status_code', resp)} {body[:200]}")
    if "signature_verification": "success" not in body:
        print("FAIL: signature verification did not succeed")
        return 1
    print("[2/5] Firing Phase 1 webhook (Telegram -> Inventory Movement)")
    try:
        print(fire_webhook(PHASE_1_URL)[:200])
    except Exception as exc:  # GAS may exceed the client read window; row still lands
        print(f"      (webhook read timed out: {exc})")
    time.sleep(10)
    print("[3/5] Firing Phase 2 webhook (Inventory Movement -> Ledgers)")
    try:
        print(fire_webhook(PHASE_2_URL)[:200])
    except Exception as exc:
        print(f"      (webhook read timed out: {exc})")
    print("[4/5] Polling Inventory Movement for marker row")
    row = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        time.sleep(POLL_SECONDS)
        row = find_row_by_marker(INVENTORY_SHEET_NAME, marker)
        if row:
            break
        print(f"      attempt {attempt}/{POLL_ATTEMPTS} ...")
    if not row:
        print("FAIL: marker row never appeared in Inventory Movement")
        return 1
    status = row[13] if len(row) > 13 else ""
    print(f"[5/5] updateId={row[0]} status(col N)={status!r}")
    if status.strip().upper() == "PROCESSED":
        print("PASS: E2E inventory movement reached PROCESSED")
        return 0
    print(f"FAIL: expected PROCESSED, got {status!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
