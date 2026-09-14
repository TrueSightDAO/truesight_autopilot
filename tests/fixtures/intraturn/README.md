# In-turn (intra-turn) compaction fixtures

Captured for **Unit 0** of `plans/SOPHIA_INTRATURN_COMPACTION_PLAN.md`
(agentic_ai_context, merged #1093).

The task: a single *open* turn that needs many tool-call rounds accumulates all
of that round-by-round output inside one turn, and the existing (inter-turn)
compaction cannot touch it. Measured live 2026-09-14: turns at **94,264** and
**72,391+** tokens within one still-open turn.

## Why these files

Pre-compaction session backups (`.pre-compact-*.json`) only ever contain
**completed** turns — the giant open turns were never written to disk mid-flight
(that is exactly the gap this plan closes). So the fixtures come from two places:

1. **Round-by-round token curves** — `raw_traces/req_<id>.log`: the full
   `journalctl` slice for each real request, showing `LLM RESP round=N
   tools=M tokens=T` climbing to 94K / 72K while still open. These are the
   ground-truth evidence that the failure mode is real.
2. **The largest real tool-heavy turns** — `open_turn_discord*.json`: extracted
   verbatim from session `434b53fd293b` (Telegram thread 27138 = Discord
   integration), the same thread the 94K/72K turns ran in. These are real
   message sequences usable as replay fixtures for the Unit 1/3 compactor.

## Contents

| File | What |
|------|------|
| `open_turn_discord.json` | 12-round real turn (34 msgs) extracted from the 04:13:48Z pre-compact snapshot |
| `open_turn_discord_long.json` | 20-round real turn (46 msgs) extracted from the live session |
| `session_434b53fd293b_20260914T041348Z.json` | whole-session snapshot taken minutes before the 94K event |
| `raw_traces/req_811177.log` | journal slice: turn reaching **94,264** tokens @ round 12 |
| `raw_traces/req_228880.log` | journal slice: turn reaching **72,391+** tokens @ round 18 |
| `raw_traces/req_979393.log` | journal slice: a further 111K-token turn (round 24) for reference |

Each `open_turn_*.json` carries `messages` plus provenance headers
(`source`, `provenance`, `turn_index_span`, `rounds`, `tokens_cc_estimate`).
