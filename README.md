# truesight_autopilot

**Unified AI service for TrueSight DAO — governor chat + autonomous SRE + developer.**

## Vision

TrueSight DAO runs on code: market research pipelines, email agents, inventory snapshots, contribution ledgers, DApp pages, tokenomics mirrors. Today, every bug, every GitHub Action failure, every AWS cost spike, every GAS execution error waits for a human to wake up, read an email, open a terminal, and fix it.

**truesight_autopilot exists to close that gap.**

It is a persistent cloud service with two modes:

### Reactive Mode — Governor Chat (`POST /chat`)
You talk to it through the DApp chat UI at `dapp.truesight.me/chat.html`:
- *"What did we ship last week?"* → reads context, summarizes PRs
- *"The circle-detect workflow failed — can you fix it?"* → diagnoses, opens PR
- *"Check my AWS costs"* → queries Cost Explorer, reports anomalies
- *"Create a PR that adds retry logic to hit_list_enrich_contact.py"* → implements, tests, opens PR

### Proactive Mode — Autopilot (background loops)
It watches continuously without human input:
- **Gmail** — polls every 5 min for GitHub Action failures, GAS errors, security alerts
- **AWS** — monitors CloudWatch metrics, Cost Explorer spend, Health events
- **GitHub** — listens to webhooks for workflow failures

Both modes share the same brain: **DeepSeek-V3** (30× cheaper than Claude) with full workspace context.

**The human stays in the loop.** The autopilot never auto-merges. Every fix is a PR. You review and merge. The service just ensures the PR is waiting for you when you check GitHub — not the error email.

## Why This Matters

| Before | After |
|---|---|
| GitHub Action fails at 3 AM → you wake up to an email → read logs → open editor → fix → commit → push | Action fails → autopilot reads email → fetches logs → diagnoses → opens PR → you merge at 9 AM |
| EC2 runs out of disk → site goes down → customer complaint → emergency SSH | Disk usage climbs → autopilot alerts → proposes resize PR → you approve |
| AWS bill surprises you at month-end | Daily cost check → anomaly detected → PR to pause non-prod resources |
| GAS execution error → manual script debugging → Stack Overflow rabbit hole | Error email → autopilot parses stack trace → proposes fix in `.gs` or Python equivalent |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         truesight_autopilot (EC2)                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  collector  │  │  classifier │  │  diagnosis  │  │   fix_generator     │ │
│  │  (pollers)  │→ │  (LLM/rules)│→ │   engine    │→ │   (code + infra)    │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│         ↑                                                    │              │
│         │                                              ┌─────┴─────┐        │
│         │                                              ▼           ▼        │
│         │                                       ┌──────────┐ ┌──────────┐  │
│         │                                       │  GitHub  │ │   AWS    │  │
│         │                                       │   PR     │ │  Action  │  │
│         │                                       └──────────┘ └──────────┘  │
│         │                                                                     │
│  ┌──────┴─────────────────────────────────────────────────────────────────┐  │
│  │                        DATA SOURCES                                     │  │
│  ├─────────────────┬─────────────────┬─────────────────┬──────────────────┤  │
│  │   Gmail IMAP    │  GitHub API     │  AWS APIs       │   GCP APIs       │  │
│  │                 │                 │                 │                  │  │
│  │ • GH Actions    │ • Workflow runs │ • CloudWatch    │ • Cloud Monitor  │  │
│  │   failures      │ • PRs / Issues  │   (EC2 metrics) │ • Billing        │  │
│  │ • GAS errors    │ • Code contents │ • Cost Explorer │ • Error Reports  │  │
│  │ • Security      │ • Dependabot    │ • EC2 status    │                  │  │
│  │   alerts        │                 │ • RDS / S3      │                  │  │
│  └─────────────────┴─────────────────┴─────────────────┴──────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   Edgar (DAO)   │
                    │  Log every fix  │
                    │  as contribution│
                    └─────────────────┘
```

## Monitors

| Source | What | Frequency | Action |
|---|---|---|---|
| **Gmail** | GitHub Action failure emails | Every 5 min | Fetch logs → diagnose → open PR |
| **Gmail** | Google Apps Script error emails | Every 5 min | Parse stack trace → propose fix |
| **GitHub API** | Workflow run status (webhook backup) | Event-driven | Same as above |
| **AWS CloudWatch** | EC2 CPU, memory, disk, status checks | Every 5 min | Alert on anomaly |
| **AWS Cost Explorer** | Daily spend by service | Daily | Report + anomaly alert |
| **AWS Health** | Regional outages affecting resources | Hourly | Alert |
| **GCP Cloud Monitoring** | GCP resource health | Every 5 min | Alert |
| **GCP Billing** | Daily GCP spend | Daily | Report + anomaly alert |

## Safety

- **Never auto-merges.** All fixes open as PRs for human review.
- **Dry-run mode.** Set `DRY_RUN=true` to print plans without writing.
- **Rate limited.** Max 5 PRs/day per repo; configurable via `MAX_PR_PER_DAY`.
- **Dedicated identity.** Edgar contributions are signed by `autopilot@agroverse.shop`, not your personal key.
- **Cost capped.** DeepSeek-V3 is ~$0.001 per diagnosis. A month of heavy use costs less than a coffee.

## Quick Start

```bash
cd truesight_autopilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# Edit .env — see SETUP.md

# Run locally (dry-run recommended first)
DRY_RUN=true python -m uvicorn app.main:app --host 0.0.0.0 --port 8001

# Check health
curl http://localhost:8001/health
```

## Deployment

Runs on the **same EC2** as `governor_chatbot_service` (`us-east-1`, t3.small):

```bash
# Edit EC2_HOST in scripts/deploy.sh, then:
./scripts/deploy.sh
```

Systemd service:
```bash
sudo systemctl status truesight-autopilot
sudo journalctl -u truesight-autopilot -f
```

## Environment

See `.env.example` for required variables. Key credentials:

| Variable | Purpose | Status |
|---|---|---|
| `TRUESIGHT_DAO_AUTOPILOT` | GitHub fine-grained PAT (Contents + PR write) | ✅ Ready |
| `GMAIL_TOKEN_JSON` | Full `token.json` from `market_research/credentials/gmail/` | ✅ Ready |
| `DEEPSEEK_API_KEY` | From platform.deepseek.com | 🆕 Sign up |
| `EMAIL` / `PUBLIC_KEY` / `PRIVATE_KEY` | Dedicated Edgar identity | 🆕 Generate via `truesight-dao-auth login` |
| `AWS_*` | Prefer IAM instance role; fallback to env vars | ❌ Need valid creds or IAM role |

Full credential audit: `agentic_ai_context/API_CREDENTIALS_DOCUMENTATION.md` §10

## How It Works (One Example)

1. `detect_circle_hosting.yml` fails at 04:17 UTC
2. GitHub emails `garyjob@agroverse.shop`: "Workflow run failed"
3. Autopilot polls Gmail, classifies as `github_failure`
4. Fetches workflow run logs via GitHub API
5. DeepSeek-V3 reads the log + `detect_circle_hosting_retailers.py`:
   ```json
   {
     "root_cause": "ModuleNotFoundError: No module named 'gspread' — dependency missing in requirements.txt",
     "proposed_fix": "Add gspread>=6.0.0 to requirements.txt",
     "files_to_edit": "requirements.txt"
   }
   ```
6. Autopilot creates branch `fix/detect-circle-hosting-missing-dep`
7. Commits the fix
8. Opens PR with diagnosis in the body
9. Logs 5-minute contribution to Edgar
10. You wake up, review the PR, click merge

## History

This repo merges two previous services:
- **`governor_chatbot_service`** — conversational AI for DAO governors (now the `/chat` endpoint)
- **`truesight_autopilot`** (original scaffold) — autonomous SRE + developer (now the background loops + proactive PRs)

Merged 2026-05-03. DeepSeek-V3 replaces Kimi + Claude for all LLM workloads. Deployed on a **dedicated EC2** separate from `seni_ror` (Edgar) to protect critical infrastructure.

## Related

- `agentic_ai_context/API_CREDENTIALS_DOCUMENTATION.md` §10 — Credential audit and readiness
- `agentic_ai_context/SETUP_REQUIREMENTS.md` — Autopilot prerequisites and blockers
- `market_research` — Primary repo the autopilot will monitor and fix
