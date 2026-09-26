# Google service-account credentials

This directory holds Google service-account JSON keys consumed by the autopilot
agent's Google Drive / Sheets / Docs tools (see `app/tools/google_*.py`).

**The JSON files themselves are gitignored** (`.gitignore` rule
`config/google/*.json`) — they are private keys and must never be committed.
This README is the only file in this directory tracked by git; it documents
which keys the autopilot expects to find here at runtime.

## Expected files

The autopilot tools resolve credentials by name in this order:

1. If the caller passes `service_account_name="<name>"`, look for
   `<name>_gdrive_key.json` then `<name>_key.json` in this directory.
2. Otherwise fall back to `$GOOGLE_APPLICATION_CREDENTIALS`.

| File | Service account | Used for |
|------|-----------------|---------|
| `cypher_defense_gdrive_key.json`            | `cypher-defense@get-data-io.iam.gserviceaccount.com` | **Default.** Main Ledger (`1GE7PUq-…`), Cypher Defense Ledger. |
| `tdg_scoring_gdrive_key.json`               | `tdg-scoring-peer-reviewer@get-data-io.iam.gserviceaccount.com` | TDG scoring sheets. |
| `upc_barcode_gdrive_key.json`               | `upc-barcode@get-data-io.iam.gserviceaccount.com` | UPC barcode sheets. |
| `edgar_dapp_listener_key.json`              | `edgar-dapp-listener@get-data-io.iam.gserviceaccount.com` | Edgar DApp Telegram logs sheet. |
| `agroverse_qr_code_manager_gdrive_key.json` | `agroverse-qr-code-manager@get-data-io.iam.gserviceaccount.com` | Agroverse QR code batches; truesight.me / agroverse.shop ops. |
| `agroverse_market_research_gdrive_key.json` | `agroverse-market-research@get-data-io.iam.gserviceaccount.com` | Market research sheets. |

Canonical reference: `agentic_ai_context/GOOGLE_API_CREDENTIALS.md`.

## Provisioning

On a fresh developer machine or new EC2 host, copy each file from the
canonical workspace location:

```bash
# Local dev: copy from the sister repos that already hold them.
cp ~/Applications/sentiment_importer/config/cypher_defense_gdrive_key.json \
   ~/Applications/sentiment_importer/config/tdg_scoring_gdrive_key.json    \
   ~/Applications/sentiment_importer/config/upc_barcode_gdrive_key.json    \
   ~/Applications/sentiment_importer/config/edgar_dapp_listener_key.json   \
   ~/Applications/truesight_autopilot/config/google/

cp ~/Applications/truesight_me/google-service-account.json \
   ~/Applications/truesight_autopilot/config/google/agroverse_qr_code_manager_gdrive_key.json

cp ~/Applications/krake_local/google-service-account.json \
   ~/Applications/truesight_autopilot/config/google/agroverse_market_research_gdrive_key.json

chmod 600 ~/Applications/truesight_autopilot/config/google/*.json
```

`scripts/deploy.sh` automatically rsyncs the contents of this directory to
`/opt/truesight_autopilot/config/google/` on the EC2 host with mode `600` —
so once a file lives here locally, the next `./scripts/deploy.sh` ships it.

## Runtime environment variables

The autopilot reads two env vars on the host:

- `GOOGLE_APPLICATION_CREDENTIALS` — absolute path to the default SA JSON.
  Recommended: `/opt/truesight_autopilot/config/google/cypher_defense_gdrive_key.json`.
- `GOOGLE_CREDS_DIR`               — directory holding the SA files.
  Recommended: `/opt/truesight_autopilot/config/google`.

Both are set in `/opt/truesight_autopilot/.env` on the EC2 host.

## Scopes

Each tool requests the narrowest scope it needs:

- `read_google_sheet` → `https://www.googleapis.com/auth/spreadsheets.readonly`
- `read_google_doc`   → `https://www.googleapis.com/auth/documents.readonly`
- `read_drive_file`, `list_drive_folder` → `https://www.googleapis.com/auth/drive.readonly`

The service accounts themselves have wider scopes — the tool-side scope list
just bounds what *this* call may do.

## Rotation

If a service-account key is compromised:
1. Revoke the old key in Google Cloud Console (`get-data-io` project).
2. Mint a new key for the same SA.
3. Replace the file here, run `./scripts/deploy.sh`, restart the service.
4. Confirm with the smoke test in `tests/test_google_sheets.py`.
