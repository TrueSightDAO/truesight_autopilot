# Gmail OAuth tokens (per account)

This directory holds Gmail OAuth-user tokens consumed by the autopilot agent's
Gmail tools (`app/tools/gmail_tools.py`).

**Token JSONs are gitignored** (`config/gmail/*.json`). They contain a
long-lived `refresh_token` — treat them like a password.

## Expected files (design intent)

| File | Mailbox (intended) | Scope |
|------|---------|-------|
| `admin_token.json` | `admin@truesight.me`     | `gmail.modify` (read + send + draft + labels) |
| `gary_token.json`  | `garyjob@agroverse.shop` | `gmail.modify` (read + send + draft + labels) |

The agent picks a mailbox via the `account` argument on each tool call —
`"admin"` (default) or `"gary"`. Default override: `GMAIL_DEFAULT_ACCOUNT`.

> ✅ **Resolved 2026-06-16 — but heed the cause.** A `getProfile` audit found that **every**
> Gmail token in the system had been authenticating as **`garyjob@agroverse.shop`**, not
> `admin@truesight.me`: `admin_token.json` (local + box), `gary_token.json`, and the vault
> entries `gmail_admin_token` / `gmail_token_admin_token` / `gmail_token_gary_token`. In other
> words `admin_token.json` was a silent duplicate of the gary token, so anything defaulting to
> `account="admin"` (incl. `app/email_poller.py`) was operating on the wrong mailbox.
>
> **Root cause:** `admin_token.json` had been copied from the legacy EC2 `GMAIL_TOKEN_JSON`,
> which is itself a `garyjob@agroverse.shop` token — never a freshly-minted `admin@truesight.me`
> one. **Fixed** by re-minting via OAuth logged in as `admin@truesight.me` and updating the
> local file, the box file, and both vault entries (`gmail_admin_token`, `gmail_token_admin_token`).
>
> **Lesson — never copy `admin_token.json` from the gary token or the legacy env.** It must be
> minted from the `admin@truesight.me` account itself (see Provisioning), and **always verified**
> with the snippet below before trusting it.

### Verify which mailbox a token reads

Always run this after minting/copying — it prints the mailbox a token *actually*
authenticates as (no secret is printed):

```bash
.venv/bin/python -c "
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
p='config/gmail/admin_token.json'
c=Credentials.from_authorized_user_file(p,['https://www.googleapis.com/auth/gmail.modify'])
print(build('gmail','v1',credentials=c,cache_discovery=False).users().getProfile(userId='me').execute()['emailAddress'])
"
# admin_token.json MUST print admin@truesight.me — if it prints garyjob@agroverse.shop it's the WRONG token.
```

## Provisioning

Tokens are minted out-of-band (one-time OAuth browser flow) and copied here.

- `gary_token.json` — `garyjob@agroverse.shop`. Source:
  `~/Applications/market_research/credentials/gmail/token.json`.
- `admin_token.json` — **must** be minted by logging into the `admin@truesight.me`
  Google account in the OAuth browser flow. **Do NOT** copy it from the legacy EC2
  `GMAIL_TOKEN_JSON` or from the gary token — both are `garyjob@agroverse.shop`
  (that mistake is the bug flagged above). After minting, run the verify snippet.

To copy the gary token into this directory:

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
