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
import json
import os
import sys
import time
import urllib.request

import gspread
from google.oauth2 import service_account
from truesight_dao_client.edgar_client import EdgarClient

OPS_SPREADSHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
MAIN_SPREADSHEET_ID = "1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU"
PROCESSED_URL = (
    "https://script.google.com/macros/s/AKfycbzWcTj3kH93i2Zj-5VlRYVpY-VD8vh0l9fDgG"
    "-Qm3bC4nWkJ7S1w4fQmBk/exec"
)
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


def find_movement_row(marker: str):
    """Find the Inventory Movement row for this marker and its STATUS (col N)."""
    ws = _client().open_by_key(OPS_SPREADSHEET_ID).worksheet("Inventory Movement")
    for row in ws.get_all_values():
        if marker in " | ".join(str(c) for c in row[:6]):
            return row
    return None


def main() -> int:
    marker = "E2E INV MOVE (Test " + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S") + ")"
    attrs = {
        "Manager Name": "Sophia Truesight",
        "Recipient Name": "Sophia Truesight",
        "QR Code": "E2E-TEST-QR",
        "Quantity": "1",
        "Inventory Item": "E2E test item - self-to-self",
        "Destination Inventory File Location": "inventory-movement (E2E test)",
    }
    print(f"[1/5] Submitting self-to-self movement marker={marker}")
    client = EdgarClient.from_env()
    resp = client.submit("INVENTORY MOVEMENT", attrs)
    body = getattr(resp, "text", "")
    print(f"      HTTP {getattr(resp, 'status_code', resp)} {body[:200]}")
    try:
        resp_json = json.loads(body)
    except Exception:
        resp_json = {}
    if resp_json.get("status") != "ok" or resp_json.get("signature_verification") != "success":
        print(f"FAIL: signature verification did not succeed (body: {body[:200]})")
        return 1
    print("[2/5] Firing Phase 1 webhook (Telegram -> Inventory Movement)")
    try:
        print(fire_webhook(PROCESSED_URL)[:200])
    except Exception as exc:  # GAS may exceed the client read window; row still lands
        print(f"      (webhook read timed out: {exc})")

    print("[3/5] Polling Inventory Movement tab for marker row")
    row = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        time.sleep(POLL_SECONDS)
        row = find_movement_row(marker)
        if row:
            break
        print(f"      attempt {attempt}/{POLL_ATTEMPTS} ...")
    if not row:
        print("FAIL: marker row never appeared in Inventory Movement tab")
        return 1
    status = row[13] if len(row) > 13 else ""
    print(f"      row status (col N)={status!r}")
    if status.strip().upper() != "PROCESSED":
        print(f"FAIL: expected status PROCESSED, got {status!r}")
        return 1

    print("[4/5] Verifying row reached PROCESSED")
    print("      PASS: self-to-self inventory movement reached PROCESSED")

    print("[5/5] Post-test state")
    print("NOTE: inventory-movement test rows are kept (they carry QR-code movement semantics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
