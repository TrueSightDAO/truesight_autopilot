# Discord Server Structure

> Canonical map of the TrueSight DAO Discord guild (`923008087315587072`).
> Last rewritten 2026-09-14 by the autopilot on Gary's instruction.

## Liveness rule (governor, 2026-09-14)

- **Any channel or category with no activity in the last 30 days is dead.** Dead
  channels are **archived** (renamed + moved into `Archive (30d+ dormant)`) — never
  deleted, so history is preserved and the move is reversible.
- **Empty folder shells** (categories whose every channel had already been archived) are
  removed. A category holds no messages, so deleting an empty category loses nothing.

This supersedes the earlier 7-category venture/game taxonomy: that layout was carried over
from the server's early days and the live whitepaper does not lead with a game/NFT model.

## Current shape (4 categories, 10 live channels, 29 archived)

| # | Category | ID | Live channels |
|---|---|---|---|
| 1 | **Our Community** | 950087920478462002 | general, south-america |
| 2 | **Agroverse** | 1548897800161464430 | agroverse-supply-chain, black-king-administration |
| 3 | **DAO Build and Ops** | 1548891663991316550 | show-typing, server-administration, member-tier, discord-parity, adapter-bugs, dapp-chat-approval-leak |
| 4 | **Archive (30d+ dormant)** | 1549111330576011456 | *(29 retired channels — see below)* |

All 10 live channels were active **the same day** as the sweep (2026-09-14).

### Why these three live categories

- **Our Community** — the public/social + regional layer that is actually in use.
- **Agroverse** — the cacao supply-chain initiative (farmers -> 10,000 ha); a distinct
  business, gets its own category.
- **DAO Build and Ops** — merged the former *Governance and Treasury* + *Build and
  Go-To-Market* + *Sophia and Server Ops* into one category, because with the 30-day rule
  only per-channel-official and operational surfaces survived: DAO/governance ops, the
  Sophia automation threads, and the Discord adapter work. There is no longer enough live
  traffic to justify three separate "work" folders.

### Renames applied this sweep

| Before | After |
|---|---|
| Sophia and Server Ops *(category)* | **DAO Build and Ops** |
| Archive *(category)* | **Archive (30d+ dormant)** |

## Archived channels (29)

Moved into `Archive (30d+ dormant)`; last activity shown (dormant >=30d, most ~1,500 days).

| # | Channel | Last activity |
|---|---|---|
| 1 | announcements-and-updates | 2022-03-14 |
| 2 | intro | 2022-06-16 |
| 3 | launchpad | *(empty)* |
| 4 | whitepaper | 2022-01-23 |
| 5 | roadmap | 2022-06-03 |
| 6 | stories-from-the-underground | 2022-06-29 |
| 7 | polls | 2022-03-26 |
| 8 | market-talks | 2022-04-20 |
| 9 | betting-pool | 2022-05-12 |
| 10 | singapore | 2022-04-29 |
| 11 | north-america | *(empty)* |
| 12 | europe | 2022-05-03 |
| 13 | japan | *(empty)* |
| 14 | south-korea | *(empty)* |
| 15 | Social Hangouts - AAA *(voice)* | *(empty)* |
| 16 | governance | 2022-04-21 |
| 17 | treasury-and-liquidity | 2022-03-29 |
| 18 | operational-updates | 2024-06-11 |
| 19 | project-goals | 2022-03-31 |
| 20 | contributions-to-be-recorded | *(empty)* |
| 21 | latest-contributions | 2022-10-25 |
| 22 | appeal-contributions | 2022-04-14 |
| 23 | engineering | 2022-06-14 |
| 24 | go-to-market | 2022-04-05 |
| 25 | nft-and-memberships | 2022-04-16 |
| 26 | nft-artworks | 2022-04-05 |
| 27 | research | 2022-05-10 |
| 28 | ux-and-design | 2022-06-24 |
| 29 | metagame-design | 2022-05-13 |

Empty categories removed this sweep (their channels had all been archived):
**Start Here**, **Governance and Treasury**, **The Metagame**, **Build and Go-To-Market**.

## Operating rules

- **Permissions:** the `sophia_truesight` bot is an **Administrator** (via the `Sophia TrueSight`
  managed role) — same authority as on Telegram, per `plans/DISCORD_ADAPTER_PLAN.md`.
  If ever tightened to least-privilege, **Manage Channels** is the single permission that
  covers creating/renaming/**deleting** both channels *and* categories ("folders").
- **Deletions:** `delete_discord_channel` requires `dry_run=false` **and** `confirm=true`.
  Prefer rename-and-move to `Archive (30d+ dormant)` over deletion. Only ever delete an
  **empty** category, or a genuinely empty channel with explicit governor approval.
- **Adapter routing:** the Discord adapter keys on **guild ID + user IDs**, and replies in
  the channel it is mentioned in — it does **not** use a channel-name/id whitelist. So
  renames, moves, and archives are routing-safe. (The T1-T3 whitelist sketched in
  `plans/DISCORD_ADAPTER_PLAN.md` §5 is *proposed*, not implemented.)
- **Tool quirk:** the create/rename tool HTML-escapes `&`; use the word "and" in channel
  names, or fix the escaping in the Discord tooling.
