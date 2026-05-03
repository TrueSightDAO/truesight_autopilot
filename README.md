# truesight_autopilot

Autonomous SRE + developer for TrueSight DAO. Monitors email, infrastructure health, and autonomously opens PRs with fixes.

## What it does

| Monitor | Source | Action |
|---|---|---|
| GitHub Action failures | Gmail (garyjob@agroverse.shop) | Fetch logs → diagnose → open PR |
| GAS execution errors | Gmail | Parse stack trace → propose fix |
| EC2 health | AWS CloudWatch | Alert on anomaly |
| AWS costs | Cost Explorer | Daily spend report + anomaly alert |
| GCP costs | Cloud Billing | Daily spend report |

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Email Poller   │────▶│  Diagnosis      │────▶│  GitHub Client  │
│  (Gmail API)    │     │  (DeepSeek-V3)  │     │  (Create PR)    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
         │                                               │
         ▼                                               ▼
┌─────────────────┐                            ┌─────────────────┐
│  AWS Monitor    │                            │  Edgar Logger   │
│  (CloudWatch)   │                            │  (Contribution) │
└─────────────────┘                            └─────────────────┘
```

## Quick Start

```bash
cd truesight_autopilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# Edit .env — see SETUP.md

# Run locally
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Deployment (EC2)

Same EC2 as `governor_chatbot_service` (us-east-1, t3.small):

```bash
# On EC2
sudo cp systemd/truesight-autopilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now truesight-autopilot
sudo systemctl status truesight-autopilot
```

## Environment

See `.env.example` for required variables. Key credentials:

- `TRUESIGHT_DAO_AUTOPILOT` — GitHub fine-grained PAT (Contents + PR write)
- `GMAIL_TOKEN_JSON` — Full `token.json` from `market_research/credentials/gmail/`
- `DEEPSEEK_API_KEY` — From platform.deepseek.com
- `EMAIL` + `PUBLIC_KEY` + `PRIVATE_KEY` — Dedicated Edgar identity (NOT personal)

## Safety

- **Never auto-merges.** All fixes open as PRs for human review.
- **Dry-run mode.** Set `DRY_RUN=true` to print plans without writing.
- **Rate limited.** Max 5 PRs/day per repo; configurable via `MAX_PR_PER_DAY`.

## Related

- `agentic_ai_context/API_CREDENTIALS_DOCUMENTATION.md` §10 — Credential audit
- `agentic_ai_context/SETUP_REQUIREMENTS.md` — Autopilot prerequisites
