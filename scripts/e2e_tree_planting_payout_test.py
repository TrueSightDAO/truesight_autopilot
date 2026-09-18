#!/usr/bin/env python3
"""E2E test: TREE PLANTING + PAYOUT events reach their ledger tabs.

Guards the CFR / SunMint payout pipeline end to end
(plans/CRF_ANAPU_SUNMINT_COHORT_PROPOSAL.md, SS12.7 Q5):

  [TREE PLANTING EVENT]  -> intake -> SunMint Tree Planting
  [PAYOUT EVENT]         -> intake -> Ops payouts        (Tier-1)
                                  -> CFR payout events   (Tier-2)

Mirrors the house convention of scripts/e2e_inventory_movement_test.py:
  * EdgarClient signs and submits the real signed event
  * the deployed GAS webhook is fired to drain the intake row
  * each target tab is polled for the test marker

SS5g compliance: every write is tagged ``e2e-tree-payout-<ts>`` in a
human-facing field, and ALL test rows are deleted in the same run.
Cleanup is mandatory; ``--keep`` exists only for debugging.

Webhook /exec URLs are capability URLs and are therefore NOT embedded here;
pass them via PAYOUT_WEBHOOK_URL and TREE_PLANTING_WEBHOOK_URL (the
DAO_PROTOCOL_WEBHOOK_*_PROCESSING values in the dao_protocol .env).

Usage:
  PAYOUT_WEBHOOK_URL=... TREE_PLANTING_WEBHOOK_URL=... \
      python3 scripts/e2e_tree_planting_payout_test.py

Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
import urllib.error
import urllib.request

import gspread
from google.oauth2 import service_account
from truesight_dao_client.edgar_client import EdgarClient

OPS_SPREADSHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
CFR_SPREADSHEET_ID = "17KwmxYOpTVR89ybRlOkDXoN9PF3UcaNu3REg2wNa83w"

INTAKE_TAB = "Telegram Chat Logs"
TREE_TAB = "SunMint Tree Planting"
PAYOUT_T1_TAB = "payouts"
PAYOUT_T2_TAB = "payout events"

CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/opt/truesight_autopilot/config/google/agroverse_qr_code_manager_gdrive_key.json",
)
# A reachable asset so the TREE PLANTING payload carries a valid Photo URL.
SAMPLE_PHOTO = (
    "https://raw.githubusercontent.com/TrueSightDAO/sunmint/main/images/"
    "20260819104739_MIIBIjANBgkqhkiG9w0B.jpg"
)
POLL_SECONDS = 10
POLL_ATTEMPTS = 6


def _client() -> gspread.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def fire_webhook(url: str, action: str) -> str:
    """GET the GAS webhook action; GAS 302s, so follow to the echo body."""
    req = urllib.request.Request(f"{url}?action={action}", method="GET")
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = resp.read().decode("utf-8", "replace")
    if body.lstrip().lower().startswith("<!doctype html"):
        raise RuntimeError(
            "webhook returned an HTML sign-in page, not JSON - the deployment "
            "for this action is auth-restricted or the env URL is stale"
        )
    return body


def rows_matching(tab: str, tag: str) -> list[tuple[int, list[str]]]:
    """Return (1-based sheet row index, row values) for every row carrying tag."""
    ws = (
        _client().open_by_key(OPS_SPREADSHEET_ID).worksheet(tab)
        if tab != PAYOUT_T2_TAB
        else _client().open_by_key(CFR_SPREADSHEET_ID).worksheet(tab)
    )
    return [
        (i, row)
        for i, row in enumerate(ws.get_all_values(), start=1)
        if any(tag in str(c) for c in row)
    ]


def delete_rows(tab: str, tag: str) -> int:
    """Delete every row carrying tag. Returns the count deleted."""
    sid = CFR_SPREADSHEET_ID if tab == PAYOUT_T2_TAB else OPS_SPREADSHEET_ID
    client = _client()
    ws = client.open_by_key(sid).worksheet(tab)
    hits = [
        i
        for i, row in enumerate(ws.get_all_values(), start=1)
        if any(tag in str(c) for c in row)
    ]
    for row_idx in sorted(hits, reverse=True):
        ws.delete_rows(row_idx)
    return len(hits)


def poll(tab: str, tag: str, label: str) -> list[list[str]]:
    for attempt in range(1, POLL_ATTEMPTS + 1):
        hits = rows_matching(tab, tag)
        if hits:
            print(f"      {label}: found {len(hits)} row(s) in {tab}")
            return [r for _, r in hits]
        time.sleep(POLL_SECONDS)
        print(f"      {label}: attempt {attempt}/{POLL_ATTEMPTS} ...")
    print(f"FAIL: no {tab} row carrying {tag}")
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="skip cleanup (debug)")
    args = ap.parse_args()

    payout_url = os.environ.get("PAYOUT_WEBHOOK_URL", "").strip()
    tree_url = os.environ.get("TREE_PLANTING_WEBHOOK_URL", "").strip()
    if not payout_url or not tree_url:
        print("FAIL: set PAYOUT_WEBHOOK_URL and TREE_PLANTING_WEBHOOK_URL")
        return 1

    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    tag = f"e2e-tree-payout-{stamp}"
    src = f"https://cfr.truesight.me/{tag}"
    results: dict[str, bool] = {}
    print(f"=== E2E tree-planting + payout  tag={tag}")

    try:
        client = EdgarClient.from_env("/opt/truesight_autopilot/.env")

        # ---- Leg 1: pseudo tree planting -------------------------------------
        print("[1/6] Submitting [TREE PLANTING EVENT]")
        resp = client.submit(
            "TREE PLANTING EVENT",
            {
                "Tree Count": "1",
                "Location": "-3.529844, -51.145114",
                "Latitude": "-3.529844",
                "Longitude": "-51.145114",
                "Species": "Cacau - Hybrid",
                "Planter": f"E2E TEST Pseudo Planter ({tag})",
                "Planting Time": datetime.datetime.utcnow().strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "Photo URL": SAMPLE_PHOTO,
                "Submission Source": src,
            },
        )
        body = getattr(resp, "text", "")
        results["tree_submitted"] = '"signature_verification":"success"' in body
        print(f"      HTTP {getattr(resp, 'status_code', resp)} {body[:160]}")

        print("[2/6] Firing tree-planting webhook")
        try:
            print(
                f"      {fire_webhook(tree_url, 'processTreePlantingTelegramLogs')[:160]}"
            )
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"      WARN tree webhook unusable: {exc}")

        print("[3/6] Polling SunMint Tree Planting")
        results["tree_row"] = bool(poll(TREE_TAB, tag, "tree"))

        # ---- Leg 2: payout to the pseudo planter -----------------------------
        print("[4/6] Submitting [PAYOUT EVENT]")
        resp = client.submit(
            "PAYOUT EVENT",
            {
                "Recipient Name": f"E2E TEST Pseudo Planter ({tag})",
                "Amount": "1.00",
                "Currency": "USD",
                "Program Slug": "crf-anapu",
                "Submission Source": src,
                "Bank Ref Type": "PIX",
                "Bank Ref": tag,
                "Paid At": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
            },
        )
        body = getattr(resp, "text", "")
        results["payout_submitted"] = '"signature_verification":"success"' in body
        print(f"      HTTP {getattr(resp, 'status_code', resp)} {body[:160]}")

        print("[5/6] Firing payout webhook")
        try:
            print(
                f"      {fire_webhook(payout_url, 'processPayoutEventsFromTelegramChatLogs')[:160]}"
            )
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"      WARN payout webhook unusable: {exc}")

        print("[6/6] Polling Ops payouts (Tier-1) + CFR payout events (Tier-2)")
        results["payout_t1"] = bool(poll(PAYOUT_T1_TAB, tag, "tier1"))
        results["payout_t2"] = bool(poll(PAYOUT_T2_TAB, tag, "tier2"))
    finally:
        if not args.keep:
            print("--- cleanup (SS5g: same-run reversal) ---")
            for tab in (INTAKE_TAB, TREE_TAB, PAYOUT_T1_TAB, PAYOUT_T2_TAB):
                try:
                    print(f"      {tab}: deleted {delete_rows(tab, tag)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"      {tab}: cleanup ERROR {exc}")
        else:
            print(f"--- --keep set; rows for {tag} left in place ---")

    failed = [k for k, v in results.items() if not v]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
