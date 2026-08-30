#!/usr/bin/env python3
"""Reusable SunMint reject/link submission — site-exact format, no hand-rolling.

Replicates the browser flow of sunmint_beta monitor-tree-growth/index.html:
  markTreeInvalid() -> signText(privateKey, requestText) -> POST shareText to Edgar.

Field order matters (2026-08-30 incident: swapping them yields
signature_verification: error + stub rows in Telegram Chat Logs):
  My Digital Signature:   <PUBLIC KEY  (SPKI, raw base64)>
  Request Transaction ID: <SIGNATURE (RSA-2048/SHA-256, raw base64)>

The signature covers ONLY requestText (the block ending at '--------'),
NOT the full shareText — same as WebCrypto signText() on the site.

Usage:
  python3 scripts/sunmint_submit.py --tree-id Edgar_20250809202528_061 [--reason "Not a valid tree"]

Keys: /tmp/sophia_keys_clean.env with PUBLIC_KEY=/PRIVATE_KEY= raw base64
(SPKI / PKCS8, NO PEM armor) — same file the E2E runbook §4.0 uses.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

EDGAR_SUBMIT_URL = "https://edgar.truesight.me/dao/submit_contribution"
DEFAULT_KEYS = "/tmp/sophia_keys_clean.env"
VERIFY_URL = "https://dapp.truesight.me/verify_request.html"


def load_keys(path: str) -> tuple[str, bytes]:
    """Return (public_key_base64, private_key_der). Accepts .env lines or JSON."""
    raw = Path(path).read_text().strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        pub = data.get("PUBLIC_KEY") or data.get("public_key") or ""
        priv = data.get("PRIVATE_KEY") or data.get("private_key") or ""
    else:

        def _get(k: str) -> str:
            for line in raw.splitlines():
                if line.startswith(k + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
            return ""

        pub = _get("PUBLIC_KEY")
        priv = _get("PRIVATE_KEY")
    if not pub or not priv:
        raise SystemExit(f"ERROR: PUBLIC_KEY/PRIVATE_KEY not found in {path}")
    try:
        priv_der = base64.b64decode(priv)
        # Validate it parses as a PKCS8/DER key before we sign with it.
        serialization.load_der_private_key(priv_der, password=None)
    except Exception as exc:  # noqa: BLE001 - surface any key-parse failure clearly
        raise SystemExit(
            f"ERROR: cannot decode PRIVATE_KEY from {path}: {exc}"
        ) from exc
    return pub, priv_der


def sign_request_text(private_key_der: bytes, request_text: str) -> str:
    """RSASSA-PKCS1-v1_5 / SHA-256 over requestText only (site signText())."""
    key = serialization.load_der_private_key(private_key_der, password=None)
    sig = key.sign(request_text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def build_share_text(
    tree_id: str,
    reason: str,
    qr_code: str,
    submitter: str,
    public_key: str,
    signature: str,
    source_url: str,
) -> str:
    request_text = (
        f"[TREE PLANTING REJECT EVENT]\n"
        f"- QR Code: {qr_code}\n"
        f"- SunMint Submission Message ID: {tree_id}\n"
        f"- Updated by: {submitter}\n"
        f"- Reason: {reason}\n"
        f"--------"
    )
    return (
        f"{request_text}\n\n"
        f"My Digital Signature: {public_key}\n\n"
        f"Request Transaction ID: {signature}\n\n"
        f"This submission was generated using {source_url}\n\n"
        f"Verify submission here: {VERIFY_URL}"
    )


def submit(share_text: str, url: str) -> tuple[int, str]:
    boundary = "----SunMintSubmit" + os.urandom(8).hex()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="text"\r\n\r\n'
        f"{share_text}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "truesight-autopilot/sunmint-submit",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - explicit DAO endpoint
        return resp.status, resp.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Submit a SunMint reject event to Edgar (site-exact format)"
    )
    ap.add_argument(
        "--tree-id",
        required=True,
        help="SunMint tree id (col A of SunMint Tree Planting tab)",
    )
    ap.add_argument("--reason", default="Not a valid tree")
    ap.add_argument(
        "--qr-code",
        default="(unlinked)",
        help="QR code if the tree is linked, else (unlinked)",
    )
    ap.add_argument("--submitter", default="Sophia Truesight", help="Updated by: value")
    ap.add_argument("--keys", default=DEFAULT_KEYS, help="Path to keypair env file")
    ap.add_argument(
        "--url",
        default=EDGAR_SUBMIT_URL,
        help="Edgar submit endpoint (override for tests)",
    )
    ap.add_argument(
        "--source-url",
        default="https://sunmint.truesight.me/monitor-tree-growth/",
        help="URL in the 'generated using' line",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Print the exact text; do NOT submit"
    )
    args = ap.parse_args()

    pub, priv_der = load_keys(args.keys)
    # Build requestText (the block the signature covers — same as the site's signText()):
    request_text_only = (
        f"[TREE PLANTING REJECT EVENT]\n"
        f"- QR Code: {args.qr_code}\n"
        f"- SunMint Submission Message ID: {args.tree_id}\n"
        f"- Updated by: {args.submitter}\n"
        f"- Reason: {args.reason}\n"
        f"--------"
    )
    signature = sign_request_text(priv_der, request_text_only)
    share_text = build_share_text(
        args.tree_id,
        args.reason,
        args.qr_code,
        args.submitter,
        pub,
        signature,
        args.source_url,
    )

    print(f"=== requestText ===\n{request_text_only}\n")
    print(f"=== shareText (first 200 chars) ===\n{share_text[:200]}...\n")
    print("=== signature_verification: pending ===")
    if args.dry_run:
        print("DRY-RUN: not submitting")
        return 0

    status, resp_body = submit(share_text, args.url)
    print(f"=== HTTP {status} ===")
    print(resp_body[:2000])
    try:
        parsed = json.loads(resp_body)
        if parsed.get("signature_verification") == "success":
            print("\n✅ SIGNATURE VERIFIED — submission ingested.")
            print("Next: fire the @37 webhook:")
            print(
                "  https://script.google.com/a/macros/agroverse.shop/s/AKfycbyoFCTzIdC1g69ZX3AK894h2siQOKoNSEiuyLDtZJTtarQPHHa5Zl8rjot0vPFUquV2/exec?action=processTreePlantingLinksFromTelegramChatLogs"
            )
        else:
            print(
                "\n⚠️ signature_verification != success — check field order / key registration."
            )
    except (json.JSONDecodeError, TypeError):
        pass
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
