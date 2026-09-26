# Contribution-time estimator (standard tool)

`scripts/estimate_contribution.py` is the **standard way** to estimate contribution
minutes for **Gary / Sophia / Envoy** from a session transcript — so we stop
hand-rolling a number every time the ask comes up.

## Why it exists

Gary's ask (thread 22433, 2026-09-05): "we probably should have a standard tool to
estimate contribution time for you, me and envoy by examining transcripts."
The policy for *how* to split time was already fixed in
`agentic_ai_context/dao/DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md`; this tool makes the
*estimation* deterministic and repeatable.

## How to run

```bash
# from the truesight_autopilot repo root
python3 scripts/estimate_contribution.py --session <12-hex-session-hash>
python3 scripts/estimate_contribution.py --session <hash> --json      # machine-readable
python3 scripts/estimate_contribution.py --session <hash> --envoy-minutes 90
```

It reads:

- `sessions/<hash>.json` → `full_history` (roles, tool ops, governor messages)
- `sessions/<hash>_debug.log` → per-LLM-round timestamps (real wall-clock raw time)

## What it emits (per seat)

| Seat | Event | Basis |
|---|---|---|
| Sophia Truesight | Raw machine execution | wall-clock across llm rounds (idle gaps >5m trimmed) from `_debug.log`; falls back to tool-ops × 18 s when no debug log |
| Sophia Truesight | Direct time (engagement/analysis) | llm rounds × 1.5 min |
| Gary Teh | Gary Teh direct time | substantive governor msgs × 3.5 min (wrapper-only msgs excluded) |
| Envoy TrueSight | (no local source) | `--envoy-minutes` override; gap tracked in OPEN_FOLLOWUPS.md |

Output ends with ready-to-submit `[CONTRIBUTION EVENT]` payload JSON
(`Type: Time (Minutes)`, `TDG Issued: 0`). **All numbers are informational —
the governor reviews and approves before any submission.**

## Calibration anchors (2026-08-24 incident, thread 14165)

Documented in `DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md`:

- ~200 tool ops ≈ 60 min raw execution → `tool_op_sec = 18`
- 17 governor messages ≈ 60 min Gary direct → `per_gov_msg_min = 3.5`

All knobs are CLI-overridable (`--tool-op-sec`, `--per-gov-msg-min`,
`--per-llm-round-min`, `--gap-trim-min`) if a session's character differs.

## Known gap: Envoy has no transcript source yet

Envoy (interactive Claude seat on `nelanco-claude`) does not publish a structured
transcript anywhere, so the tool cannot estimate Envoy autonomously. Until that
source exists, Envoy minutes are supplied via `--envoy-minutes` (Gary provides the
figure from the interactive session). Tracked in agentic_ai_context/OPEN_FOLLOWUPS.md.
