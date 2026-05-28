# Gmail OAuth tokens (per account)

This directory holds Gmail OAuth-user tokens consumed by the autopilot agent's
Gmail tools (`app/tools/gmail_tools.py`).

**Token JSONs are gitignored** (`config/gmail/*.json`). They contain a
long-lived `refresh_token` — treat them like a password.

## Expected files

| File | Mailbox | Scope |
|------|---------|-------|
| `admin_token.json` | `admin@truesight.me`     | `gmail.modify` (read + send + draft + labels) |
| `gary_token.json`  | `garyjob@agroverse.shop` | `gmail.modify` (read + send + draft + labels) |

The agent picks a mailbox via the `account` argument on each tool call —
`"admin"` (default) or `"gary"`. Default override: `GMAIL_DEFAULT_ACCOUNT`.

## Provisioning

Tokens are minted out-of-band (one-time OAuth browser flow) and copied here.
For convenience, the two existing tokens live at:

- `~/Applications/market_research/credentials/gmail/token.json` →
  `gary_token.json` (this is the `garyjob@agroverse.shop` mailbox)
- The legacy `GMAIL_TOKEN_JSON` env var on EC2 →
  `admin_token.json` (this is the `admin@truesight.me` mailbox)

To copy them into this directory:

```bash
cp ~/Applications/market_research/credentials/gmail/token.json \
   ~/Applications/truesight_autopilot/config/gmail/gary_token.json
chmod 600 ~/Applications/truesight_autopilot/config/gmail/*_token.json
```

To re-mint after a scope change or revocation, see
`agentic_ai_context/GMAIL_OAUTH_WORKFLOW.md` and the local script
`market_research/scripts/gmail_oauth_authorize.py`.

`scripts/deploy.sh` rsyncs the contents of this directory to
`/opt/truesight_autopilot/config/gmail/` with `chmod 600` on every deploy.

## Runtime environment variables

- `GMAIL_TOKENS_DIR` — directory holding `{account}_token.json` files.
  Default: `/opt/truesight_autopilot/config/gmail`.
- `GMAIL_DEFAULT_ACCOUNT` — fallback when a tool call doesn't pass `account`.
  Default: `"admin"`.
- `GMAIL_TOKEN_JSON` — **legacy** single-account env var. Still read by
  `app/email_poller.py` and falls through to `"admin"` if
  `admin_token.json` is missing. Keep it set during migration.

## Scopes

All tokens here use `https://www.googleapis.com/auth/gmail.modify`. That single
scope covers search, read, send, draft create/update/delete, and label
modify — i.e. everything the autopilot's Gmail tools currently expose. If a
future tool needs `gmail.settings.basic` or `gmail.metadata`, mint a new token
with the broader scope, replace the file here, re-deploy.

## Rotation

If a token leaks or you want to invalidate one:

1. Revoke the OAuth grant at <https://myaccount.google.com/permissions>.
2. Re-mint via `market_research/scripts/gmail_oauth_authorize.py` (or the
   equivalent for that account).
3. Replace the file here, run `./scripts/deploy.sh`, restart the service.
4. Quick check from EC2:
   `/opt/truesight_autopilot/.venv/bin/python -c "from app.tools.gmail_tools import gmail_search; print(gmail_search('newer_than:1d', account='gary', max_results=1))"`
