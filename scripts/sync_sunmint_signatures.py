#!/usr/bin/env python3
"""Publish a public, auditable JSON cache of ALL RSA-signed DAO events.

Writes one immutable JSON file per event to TrueSightDAO/verify_public_signatures
(api_only data repo, Contents-API PUTs), bucketed by event type:

  tree_planting/<msg_id>.json            -- one file per [TREE PLANTING EVENT]
  tree_growth_monitoring/<msg_id>.json   -- one file per [TREE GROWTH MONITORING EVENT] /
                                            per Tree Growth Measurements row (public link-share)
  tree_planting_link/<msg_id>.json       -- [TREE PLANTING LINK EVENT]
  tree_planting_reject/<msg_id>.json     -- [TREE PLANTING REJECT EVENT]
  <folder>/index.json                    -- per-folder registry (message_id -> url, submitted_at)
  index.json                             -- org-wide root index (event_type -> count, links)

Each event file carries a self-verifying triple: public_key, signature, signed_payload
(the EXACT bytes signed), so anyone can re-verify offline with openssl.

PII policy (governor decision 2026-09-02, Gary): option 3 -- publish signed_text AS-IS,
emails INCLUDED, so every signature verifies over the exact bytes Edgar checked. Emails are
already public in the Telegram Chat Logs that back the DAO's public dapp. The build defaults
to FAIL-CLOSED (email scan blocks publication); pass --allow-pii to publish as-is. The cron
runs with --allow-pii per this decision.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json python3 scripts/sync_sunmint_signatures.py --dry-run
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json GITHUB_TOKEN=... python3 scripts/sync_sunmint_signatures.py --push
    (without --push the per-event ledger is written locally to ./_ledger/)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib

_PUSHED_THIS_RUN: list = []
import json
import os
import re
import sys
import time
import urllib.request

SOURCE_SHEET_ID = "1qbZZhf-_7xzmDTriaJVWj6OZshyQsFkdsAV8-pyzASQ"
CHAT_LOGS_TAB = "Telegram Chat Logs"
PLANTING_TAB = "SunMint Tree Planting"
GROWTH_TAB = "Tree Growth Measurements"
GH_API = "https://api.github.com/repos/TrueSightDAO/verify_public_signatures/contents/"

# Every RSA-signed event marker is in scope (any text carrying both
# "My Digital Signature:" and "Request Transaction ID:" lines). Events are
# auto-bucketed to folders by marker (see _folder_for).

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


def parse_event(text: str):
    """Return {marker, public_key, signature, payload} if text is an RSA-signed event."""
    m = re.match(r"\s*(\[[^\]]+\])", text)
    if not m:
        return None
    marker = m.group(1)
    pub = re.search(r"My Digital Signature:\s*([^\n]+)", text)
    sig = re.search(r"Request Transaction ID:\s*([^\n]+)", text)
    if not pub or not sig:
        return None  # not an RSA-signed event
    return {
        "marker": marker,
        "public_key": pub.group(1).strip() if pub else "",
        "signature": sig.group(1).strip() if sig else "",
        "payload": _signed_payload(text),
    }


def build_signatures(
    chat_rows: list,
    planting_by_msg: dict,
    growth_by_msg: dict,
    allow_pii: bool = False,
) -> dict:
    events = {}
    test_events = {}
    other_signed = {}
    excluded_pii = {}
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
        if _is_test_event(text, msg_id):
            test_events[msg_id] = {
                "event_type": parsed["marker"],
                "telegram_message_id": msg_id,
                "reason": "test/synthetic (SYNTH source, E2ETEST id, localhost provenance)",
                "public_key": parsed["public_key"],
                "signature": parsed["signature"],
                "signed_payload": parsed["payload"],
            }
            continue
        if not allow_pii and EMAIL_RE.search(text):
            excluded_pii[msg_id] = {
                "event_type": parsed["marker"],
                "telegram_message_id": msg_id,
                "reason": (
                    "email address embedded in signed_text -- excluded per"
                    " governor decision 2026-09-02 (option 2: exclude PII events)"
                ),
                "public_key": parsed["public_key"],
                "signature": parsed["signature"],
            }
            continue
        if not parsed["public_key"].startswith(_SPKI_PREFIX):
            # Real attestation but NOT RSA-2048 (e.g. reviewer sha256 signing keys
            # on CONTRIBUTION REVIEW EVENT). Keep out of the RSA ledger but do NOT
            # label it test/malformed.
            other_signed[msg_id] = {
                "event_type": parsed["marker"],
                "telegram_message_id": msg_id,
                "reason": "non-RSA key type (not an RSA-2048 SPKI public key)",
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
        "excluded_pii_count": len(excluded_pii),
        "excluded_pii_events": excluded_pii,
        "other_signed_count": len(other_signed),
        "other_signed": other_signed,
        "events": events,
        "warnings": {
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


def _count_emails(obj) -> int:
    """Count email-like pattern hits (used for --allow-pii reporting)."""
    n = 0
    if isinstance(obj, dict):
        for v in obj.values():
            n += _count_emails(v)
    elif isinstance(obj, list):
        for v in obj:
            n += _count_emails(v)
    elif isinstance(obj, str):
        n += len(EMAIL_RE.findall(obj))
    return n


def _git_blob_sha(content: str) -> str:
    """git blob sha1 of the exact bytes we would upload."""
    raw = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def _upload(path: str, payload: dict) -> bool:
    """PUT one ledger file; returns True if a new commit was written.

    Content-addressed skip: compute the git blob sha of the exact bytes we
    would upload and compare with the remote file's sha. If equal, the file
    is already current -- skip the PUT entirely (GitHub's Contents API does
    NOT reliably no-op identical-content PUTs; it often creates a new commit
    with a fresh sha every time, which made the trickle backfill re-push the
    same first N files on every cron pass).
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("--push needs GITHUB_TOKEN or GH_TOKEN")
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    local_sha = _git_blob_sha(body)
    remote_sha = None
    try:
        req0 = urllib.request.Request(GH_API + path, method="GET")
        req0.add_header("Authorization", f"Bearer {token}")
        req0.add_header("Accept", "application/vnd.github+json")
        with urllib.request.urlopen(req0, timeout=30) as r0:
            remote_sha = json.load(r0).get("sha")
    except urllib.error.HTTPError:
        pass  # file may not exist yet -- PUT without sha creates it
    if remote_sha == local_sha:
        print(f"[skip] {path} -> already current (blob sha match)")
        return False
    data = json.dumps(
        {
            "message": f"cache(scripts): refresh {path} (sync_sunmint_signatures.py)",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": remote_sha,
        }
    ).encode()
    req = urllib.request.Request(GH_API + path, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[push] {path} -> {json.load(r).get('commit', {}).get('sha', '?')}")
            _PUSHED_THIS_RUN.append(path)
            return True
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print(f"[skip] {path} -> already current (422 unchanged)")
            return False
        raise


EVENT_FOLDER = {
    "[TREE PLANTING EVENT]": "tree_planting",
    "[TREE GROWTH MONITORING EVENT]": "tree_growth_monitoring",
    "[TREE PLANTING LINK EVENT]": "tree_planting_link",
    "[TREE PLANTING REJECT EVENT]": "tree_planting_reject",
}

RAW_BASE = (
    "https://raw.githubusercontent.com/TrueSightDAO/verify_public_signatures/main/"
)


def _folder_for(marker: str) -> str:
    """Map an event marker to its ledger folder (snake_case slug)."""
    if marker in EVENT_FOLDER:
        return EVENT_FOLDER[marker]
    slug = marker.strip("[]").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug or "other"


def _ledger_files(signatures: dict, measurements: dict) -> dict:
    """Return {repo_path: payload} for the per-event ledger layout.

    tree_planting/<msg_id>.json etc. -- one immutable file per event,
    plus <folder>/index.json per folder and a root index.json.
    Per-folder counts are derived from the ACTUAL event files written, so
    a folder never reports a count that disagrees with its contents
    (e.g. growth events present in chat logs while the tab is still empty).
    """
    folders: dict[str, dict] = {}
    events = signatures.get("events", {})
    for msg_id, ev in events.items():
        folder = _folder_for(ev.get("event_type", ""))
        folders.setdefault(folder, {})[msg_id] = ev

    # Tree Growth Measurements -> tree_growth_monitoring/ (public link-share, 0.4).
    # Enrich the event record with the measurement fields when the tab has rows;
    # keep a uniform event_type so the per-folder schema stays consistent.
    for m in measurements.get("items", []):
        mid = m.get("telegram_message_id", "")
        if not mid:
            continue
        rec = dict(m)
        rec.setdefault("event_type", "[TREE GROWTH MONITORING EVENT]")
        folders.setdefault("tree_growth_monitoring", {})[mid] = rec

    files: dict[str, dict] = {}
    for folder, evs in sorted(folders.items()):
        entries = {
            mid: {
                "url": f"{RAW_BASE}{folder}/{mid}.json",
                "event_type": ev.get("event_type", ""),
                "submitted_at": ev.get("submitted_at", ""),
                "contributor_name": ev.get("contributor_name", ""),
            }
            for mid, ev in sorted(evs.items())
        }
        for mid, ev in sorted(evs.items()):
            files[f"{folder}/{mid}.json"] = ev
        files[f"{folder}/index.json"] = {
            "status": "success",
            "schema_version": 1,
            "generated_at": _now_iso(),
            "event_type": next(iter(entries.values()), {}).get("event_type", ""),
            "count": len(entries),
            "events": entries,
        }

    # Non-RSA attestations (e.g. reviewer sha256 keys) get their own honest folder.
    for mid, ev in signatures.get("other_signed", {}).items():
        folders.setdefault("other_signed_events", {})[mid] = ev

    files["index.json"] = {
        "status": "success",
        "schema_version": 1,
        "generated_at": _now_iso(),
        "total_count": sum(len(evs) for evs in folders.values()),
        "test_events_count": signatures.get("test_events_count", 0),
        "other_signed_count": signatures.get("other_signed_count", 0),
        "event_types": {
            folder: {
                "count": len(evs),
                "index_url": f"{RAW_BASE}{folder}/index.json",
            }
            for folder, evs in sorted(folders.items())
        },
    }
    return files


def _write_ledger_local(files: dict) -> None:
    for path in sorted(files):
        local = os.path.join("_ledger", path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w") as f:
            json.dump(files[path], f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[local] wrote ./_ledger/{path}")


def _load_cursor(cursor_path: str) -> str:
    if not cursor_path:
        return ""
    try:
        with open(cursor_path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _save_cursor(cursor_path: str, path: str) -> None:
    if not cursor_path:
        return
    try:
        with open(cursor_path, "w") as fh:
            fh.write(path)
    except OSError as e:
        print(f"[warn] could not write cursor {cursor_path}: {e}")


def _push_ledger(
    files: dict,
    max_uploads: int = 250,
    cursor_path: str = "",
) -> None:
    """Push ledger files, capped per run to stay well under GitHub rate limits.

    Trickle backfill: each 30-min cron pass uploads at most max_uploads NEW
    files (default 250), then stops. Progress is persisted in a cursor file so
    the NEXT pass resumes where this one left off -- the loop never restarts
    from the top (which would re-skip the same already-pushed files forever
    and never advance, the original stuck-backfill bug).

    Idempotent and self-healing: every file is GET-checked (sha-aware skip)
    before PUT; an interrupted pass just resumes from the cursor next run.
    """
    _PUSHED_THIS_RUN.clear()
    paths = sorted(files)
    cursor = _load_cursor(cursor_path)
    start = 0
    if cursor:
        for i, p in enumerate(paths):
            if p > cursor:
                start = i
                break
        else:
            start = len(paths)  # cursor past the end -> nothing to do
    pushed = 0
    examined = 0
    for i in range(start, len(paths)):
        path = paths[i]
        if pushed >= max_uploads:
            remain = len(paths) - i
            _save_cursor(cursor_path, paths[i - 1] if i > 0 else "")
            print(
                f"[info] rate-limit guard: hit {max_uploads}/run cap; "
                f"{remain} files remain for next cron pass(es)"
            )
            break
        _upload(path, files[path])
        examined += 1
        pushed = len(_PUSHED_THIS_RUN)
        _save_cursor(cursor_path, path)
        time.sleep(0.3)
    else:
        # finished the whole set
        _save_cursor(cursor_path, "")
        print(
            f"[info] backfill complete: pushed {pushed}, examined {examined}, "
            f"cursor cleared"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--push", action="store_true")
    p.add_argument(
        "--max-uploads",
        type=int,
        default=250,
        help="Cap PUTs per run (default 250) to avoid GitHub rate limits; "
        "the 30-min cron trickles the rest on later passes.",
    )
    p.add_argument(
        "--allow-pii",
        action="store_true",
        help="Governor decision 2026-09-02 (Gary): publish signed_text as-is, "
        "emails included, preserving signature verification. Without this flag "
        "the build fails closed on any email-like pattern.",
    )
    args = p.parse_args()

    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds or not os.path.isfile(creds):
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS must point at a service account JSON")

    import gspread  # lazy: keeps test imports (CI has no gspread) working

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

    signatures = build_signatures(
        chat_rows, planting_by_msg, growth_by_msg, allow_pii=args.allow_pii
    )
    measurements = build_measurements(grow_rows, chat_by_msg)
    print(
        f"[info] signatures: {signatures['count']}  measurements: {measurements['count']}"
    )
    if signatures["excluded_pii_count"]:
        print(
            f"[info] excluded {signatures['excluded_pii_count']} PII-bearing events"
            f" (email embedded -- governor decision 2026-09-02)"
        )

    files = _ledger_files(signatures, measurements)
    if args.allow_pii:
        n = _count_emails(files)
        print(
            f"[info] --allow-pii (governor decision 2026-09-02): publishing as-is; "
            f"{n} email-like hits across {len(files)} ledger files"
        )
    else:
        for path in files:
            _scan(files[path])
        print(
            f"[info] PII scan passed across {len(files)} ledger files "
            f"(no email-like patterns)"
        )

    if args.push:
        _push_ledger(
            files,
            max_uploads=args.max_uploads,
            cursor_path=os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".ledger_cursor"
            ),
        )
    else:
        _write_ledger_local(files)


if __name__ == "__main__":
    main()
