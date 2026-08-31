#!/usr/bin/env python3
"""Publish a public, auditable JSON cache of ALL SunMint RSA-signed events.

Writes two public files to TrueSightDAO/sunmint (api_only data repo, Contents-API PUTs):

  signatures.json                  -- every SunMint-associated RSA-signed event keyed by
                                      Telegram Message ID: event_type, submitted_at,
                                      contributor_name, public_key, signature, signed_text
                                      (a self-verifying triple) + source_tab + linked_tree_id.
  tree_growth_measurements.json    -- public link-share of the Tree Growth Measurements tab
                                      (one entry per measurement row, dedup by message ID),
                                      each enriched with the farmer's signature + signed_text
                                      joined back from Telegram Chat Logs so it is
                                      independently re-verifiable with openssl.

NO PII: [EMAIL VERIFICATION EVENT] / [EMAIL REGISTERED EVENT] bodies CONTAIN the farmer's
email address inside signed_text, and redacting signed_text would break signature
verification -- so those events are deliberately EXCLUDED from the public cache (decision
0.2/0.3 update, flagged to Gary 2026-09-01). Everything else in scope carries only public
keys (already public via Contributors Digital Signatures), signatures, display names
(already public on the impact map) and tree/geo data (already public in sunmint geojson).
A fail-closed email scan runs on every build before anything is written or pushed.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json python3 scripts/sync_sunmint_signatures.py --dry-run
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json GITHUB_TOKEN=... python3 scripts/sync_sunmint_signatures.py --push
    (without --push the two JSON files are written locally to ./)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sys
import urllib.request

import gspread

SOURCE_SHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
CHAT_LOGS_TAB = "Telegram Chat Logs"
PLANTING_TAB = "SunMint Tree Planting"
GROWTH_TAB = "Tree Growth Measurements"
GH_API = "https://api.github.com/repos/TrueSightDAO/sunmint/contents/"

# SunMint-associated event markers in scope (all RSA-signed).
# NOTE: EMAIL VERIFICATION/REGISTERED events are intentionally EXCLUDED -- their
# signed_text contains the farmer's email address (PII); redacting it would break
# signature verification, so the events are simply not published.
SUNMINT_MARKERS = (
    "[TREE PLANTING EVENT]",
    "[TREE GROWTH MONITORING EVENT]",
    "[TREE PLANTING REJECT EVENT]",
    "[TREE PLANTING LINK EVENT]",
)

# Telegram Chat Logs column indices (0-based). Header row is detected at runtime --
# row 1 of this tab is a junk cell, the real header is row 2.
CHAT = {
    "update_id": 0,
    "msg_id": 3,
    "contributor": 4,
    "contribution": 6,
    "status_date": 11,
}
# SunMint Tree Planting tab columns (0-based, header row = row 0).
PLANT = {
    "update_id": 0,
    "msg_id": 3,
    "contribution": 5,
    "submitted_name": 9,
    "latitude": 10,
    "longitude": 11,
    "status": 12,
    "species": 13,
    "linked_qr": 17,
}
# Tree Growth Measurements tab columns.
GROW = {
    "update_id": 0,
    "msg_id": 1,
    "tree_id": 2,
    "species": 3,
    "dbh": 4,
    "agb": 5,
    "co2e": 6,
    "latitude": 7,
    "longitude": 8,
    "measured_at": 9,
    "closeup": 10,
    "context": 11,
    "analysis_url": 12,
    "analysis_sha": 13,
    "farmer_sig": 14,
    "contributor": 15,
    "status": 16,
    "processed": 17,
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(row: list, key: str, colmap: dict) -> str:
    idx = colmap[key]
    return (row[idx] if idx < len(row) else "").strip()


def _iso_date(yyyymmdd: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", yyyymmdd.strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else yyyymmdd.strip()


def _load_rows(ws) -> list:
    """Return data rows below the header. Chat logs has a junk row 1; tabs have the
    header at row 0 -- locate the row containing the 'Telegram Message ID' header."""
    rows = ws.get_all_values()
    start = 0
    for i, r in enumerate(rows[:5]):
        if any(str(c).strip() == "Telegram Message ID" for c in r):
            start = i
            break
    return rows[start + 1 :]


def _signed_payload(text: str) -> str:
    """Extract the EXACT bytes that were signed (mirrors signature_verifier.rb):
    everything up to and including the -------- separator, normalized to \n, stripped.
    This is the string `openssl dgst -sha256 -verify` must succeed against."""
    normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")
    sep = -1
    for i, line in enumerate(lines):
        if line.strip() == "--------":
            sep = i
            break
    if sep == -1:
        return ""
    return "\n".join(lines[0 : sep + 1]).strip()


_SPKI_PREFIX = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"  # RSA-2048 SPKI DER base64 head


def _is_test_event(text: str, msg_id: str) -> bool:
    """Test/synthetic events carry placeholder keys or localhost provenance —
    they must never mix with real farmer attestations in the public cache."""
    if msg_id.startswith(("E2ETEST_", "TEST-")):
        return True
    if "Submission Source: SYNTH" in text:
        return True
    if "generated using http://localhost" in text:
        return True
    return False


_SPKI_PREFIX = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"  # RSA-2048 SPKI DER base64 head


def _is_test_event(text: str, msg_id: str) -> bool:
    """Test/synthetic events carry placeholder keys or localhost provenance —
    they must never mix with real farmer attestations in the public cache."""
    if msg_id.startswith(("E2ETEST_", "TEST-")):
        return True
    if "Submission Source: SYNTH" in text:
        return True
    if "generated using http://localhost" in text:
        return True
    return False


def parse_event(text: str):
    """Return {marker, public_key, signature, payload} if text is an in-scope SunMint event."""
    m = re.match(r"\s*(\[[^\]]+\])", text)
    if not m:
        return None
    marker = m.group(1)
    if marker not in SUNMINT_MARKERS:
        return None
    pub = re.search(r"My Digital Signature:\s*([^\n]+)", text)
    sig = re.search(r"Request Transaction ID:\s*([^\n]+)", text)
    return {
        "marker": marker,
        "public_key": pub.group(1).strip() if pub else "",
        "signature": sig.group(1).strip() if sig else "",
        "payload": _signed_payload(text),
    }


def build_signatures(
    chat_rows: list, planting_by_msg: dict, growth_by_msg: dict
) -> dict:
    events = {}
    test_events = {}
    dupes = []
    for row in chat_rows:
        msg_id = _cell(row, "msg_id", CHAT)
        if not msg_id:
            continue
        text = _cell(row, "contribution", CHAT)
        parsed = parse_event(text)
        if not parsed:
            continue
        if msg_id in events:
            dupes.append(msg_id)
            continue
        source_tabs = []
        if msg_id in planting_by_msg:
            source_tabs.append(PLANTING_TAB)
        if msg_id in growth_by_msg:
            source_tabs.append(GROWTH_TAB)
        if not source_tabs:
            source_tabs.append(CHAT_LOGS_TAB)
        linked_tree_id = ""
        if msg_id in growth_by_msg:
            linked_tree_id = growth_by_msg[msg_id].get("tree_id", "")
        elif msg_id in planting_by_msg:
            linked_tree_id = planting_by_msg[msg_id].get("linked_qr", "")
        contributor = ""
        if msg_id in planting_by_msg:
            contributor = planting_by_msg[msg_id].get("submitted_name", "")
        if not contributor:
            contributor = _cell(row, "contributor", CHAT)
        if _is_test_event(text, msg_id) or not parsed["public_key"].startswith(
            _SPKI_PREFIX
        ):
            test_events[msg_id] = {
                "event_type": parsed["marker"],
                "telegram_message_id": msg_id,
                "reason": (
                    "test/synthetic (SYNTH source, E2ETEST id, localhost provenance)"
                    if _is_test_event(text, msg_id)
                    else "malformed/unverifiable (public key is not an RSA-2048 SPKI key)"
                ),
                "public_key": parsed["public_key"],
                "signature": parsed["signature"],
                "signed_payload": parsed["payload"],
            }
            continue
        events[msg_id] = {
            "event_type": parsed["marker"],
            "telegram_message_id": msg_id,
            "telegram_update_id": _cell(row, "update_id", CHAT),
            "submitted_at": _iso_date(_cell(row, "status_date", CHAT)),
            "contributor_name": contributor,
            "public_key": parsed["public_key"],
            "signature": parsed["signature"],
            "signed_payload": parsed["payload"],
            "signed_text": text,
            "source_tab": ", ".join(source_tabs),
            "verifiable": (
                parsed["public_key"].startswith(_SPKI_PREFIX)
                and len(parsed["signature"]) >= 300
            ),
            "linked_tree_id": linked_tree_id,
        }
    return {
        "status": "success",
        "generated_at": _now_iso(),
        "schema_version": 1,
        "count": len(events),
        "test_events_count": len(test_events),
        "test_events": test_events,
        "events": events,
        "warnings": {
            "excluded_email_events": "EMAIL VERIFICATION/REGISTERED excluded (PII)",
            "duplicate_msg_ids": dupes,
        },
    }


def build_measurements(growth_rows: list, chat_by_msg: dict) -> dict:
    items = []
    for row in growth_rows:
        msg_id = _cell(row, "msg_id", GROW)
        if not msg_id:
            continue
        chat = chat_by_msg.get(msg_id, {})
        items.append(
            {
                "telegram_message_id": msg_id,
                "tree_id": _cell(row, "tree_id", GROW),
                "species": _cell(row, "species", GROW),
                "dbh_cm": _cell(row, "dbh", GROW),
                "agb_kg": _cell(row, "agb", GROW),
                "co2e_kg": _cell(row, "co2e", GROW),
                "latitude": _cell(row, "latitude", GROW),
                "longitude": _cell(row, "longitude", GROW),
                "measured_at": _cell(row, "measured_at", GROW),
                "closeup_photo_url": _cell(row, "closeup", GROW),
                "context_photo_url": _cell(row, "context", GROW),
                "analysis_commit_url": _cell(row, "analysis_url", GROW),
                "analysis_sha256": _cell(row, "analysis_sha", GROW),
                "farmer_public_key": _cell(row, "farmer_sig", GROW),
                "signature": chat.get("signature", ""),
                "signed_payload": chat.get("payload", ""),
                "signed_text": chat.get("signed_text", ""),
                "contributor_name": _cell(row, "contributor", GROW),
                "status": _cell(row, "status", GROW),
                "processed_at": _cell(row, "processed", GROW),
            }
        )
    return {
        "status": "success",
        "generated_at": _now_iso(),
        "schema_version": 1,
        "count": len(items),
        "items": items,
    }


def _assert_no_email(value: str, path: str) -> None:
    if EMAIL_RE.search(value):
        raise SystemExit(
            f"PII BLOCKED: email-like pattern at {path} -- refusing to publish"
        )


def _scan(obj, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        _assert_no_email(obj, path)


def _upload(path: str, payload: dict) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("--push needs GITHUB_TOKEN or GH_TOKEN")
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    sha = None
    try:
        req0 = urllib.request.Request(GH_API + path, method="GET")
        req0.add_header("Authorization", f"Bearer {token}")
        req0.add_header("Accept", "application/vnd.github+json")
        with urllib.request.urlopen(req0, timeout=30) as r0:
            sha = json.load(r0).get("sha")
    except urllib.error.HTTPError:
        pass  # file may not exist yet -- PUT without sha creates it
    data = json.dumps(
        {
            "message": f"cache(scripts): refresh {path} (sync_sunmint_signatures.py)",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": sha,
        }
    ).encode()
    req = urllib.request.Request(GH_API + path, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[push] {path} -> {json.load(r).get('commit', {}).get('sha', '?')}")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print(f"[push] {path} -> unchanged (already current)")
        else:
            raise


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--push", action="store_true")
    args = p.parse_args()

    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds or not os.path.isfile(creds):
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS must point at a service account JSON")

    gc = gspread.service_account(filename=creds)
    sh = gc.open_by_key(SOURCE_SHEET_ID)
    chat_rows = _load_rows(sh.worksheet(CHAT_LOGS_TAB))
    plant_rows = _load_rows(sh.worksheet(PLANTING_TAB))
    grow_rows = _load_rows(sh.worksheet(GROWTH_TAB))
    print(
        f"[info] rows: chat={len(chat_rows)} planting={len(plant_rows)} growth={len(grow_rows)}"
    )

    planting_by_msg = {}
    for r in plant_rows:
        m = _cell(r, "msg_id", PLANT)
        if m:
            planting_by_msg[m] = {k: _cell(r, k, PLANT) for k in PLANT}
    growth_by_msg = {}
    for r in grow_rows:
        m = _cell(r, "msg_id", GROW)
        if m:
            growth_by_msg[m] = {k: _cell(r, k, GROW) for k in GROW}
    chat_by_msg = {}
    for r in chat_rows:
        m = _cell(r, "msg_id", CHAT)
        if m:
            parsed = parse_event(_cell(r, "contribution", CHAT))
            chat_by_msg[m] = {
                "signature": parsed["signature"] if parsed else "",
                "payload": parsed["payload"] if parsed else "",
                "signed_text": _cell(r, "contribution", CHAT),
            }

    signatures = build_signatures(chat_rows, planting_by_msg, growth_by_msg)
    measurements = build_measurements(grow_rows, chat_by_msg)
    print(
        f"[info] signatures: {signatures['count']}  measurements: {measurements['count']}"
    )

    _scan(signatures)
    _scan(measurements)
    print("[info] PII scan passed (no email-like patterns)")

    for path, payload in (
        ("signatures.json", signatures),
        ("tree_growth_measurements.json", measurements),
    ):
        if args.push:
            _upload(path, payload)
        else:
            with open(path, "w") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"[local] wrote ./{path}")


if __name__ == "__main__":
    main()
