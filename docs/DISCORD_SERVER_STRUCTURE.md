# Discord Server Structure

> Canonical map of the TrueSight DAO Discord guild (`923008087315587072`).
> Rewritten 2026 by the autopilot on Gary's instruction: the server had been dormant
> for years and was still organized around a 4-category layout from its early days.

## Why this shape (whitepaper basis)

The taxonomy follows the whitepaper / mission rather than legacy labels:

- **Start Here** — first-touch surface: identity, announcements, roadmap, whitepaper.
- **Our Community** — the public/regional social layer.
- **Governance and Treasury** — the DAO's decision + accounting surfaces (contributions,
  appeals, treasury, operational updates).
- **Agroverse** — the cacao supply-chain initiative (farmers -> 10,000 ha) is a *distinct*
  business and gets its own category instead of sharing "Building our DAO".
- **The Metagame** — the DAO's own term for the venture-building experiment: NFT/membership
  design and play/UX surfaces.
- **Build and Go-To-Market** — the cross-cutting "work" category (engineering, GTM).
- **Sophia and Server Ops** — operational/automation surfaces (server admin, member tier,
  Sophia working threads).

## Before -> After

### Before (4 categories; 10 uncategorized channels)

| Category | Channels |
|---|---|
| Our Community | launchpad, general, stories-from-the-underground, polls, market-talks, singapore, north-america, south-america, europe, japan, south-korea |
| Building our DAO | operational-updates, project-goals, whitepaper, governance, treasury-and-liquidity |
| Building our Game | nft-planning, nft-artworks, research, ux-and-design, marketing, engineering, game-design |
| Sophia working threads | show-typing, server-administration, member-tier |
| *(uncategorized)* | Social Hangouts - AAA (voice), intro, betting-pool, roadmap, latest-contributions, appeal-contributions, contributions-to-be-recorded, announcements-and-updates, agroverse-supply-chain, black-king-administration |

### After (7 categories, ordered by audience)

| # | Category | ID | Channels |
|---|---|---|---|
| 1 | Start Here | 1548897798341001236 | announcements-and-updates, intro, launchpad, whitepaper, roadmap, stories-from-the-underground |
| 2 | Our Community | 950087920478462002 | general, polls, market-talks, betting-pool, singapore, north-america, south-america, europe, japan, south-korea, Social Hangouts - AAA (Ask Anyone Anything) *(voice)* |
| 3 | Governance and Treasury | 953325803788173372 | governance, treasury-and-liquidity, operational-updates, project-goals, contributions-to-be-recorded, latest-contributions, appeal-contributions |
| 4 | Agroverse | 1548897800161464430 | agroverse-supply-chain, black-king-administration |
| 5 | The Metagame | 950088018658738217 | nft-and-memberships, nft-artworks, research, ux-and-design, metagame-design |
| 6 | Build and Go-To-Market | 1548897801553977385 | engineering, go-to-market |
| 7 | Sophia and Server Ops | 1548891663991316550 | show-typing, server-administration, member-tier |

### Category renames

| Old | New |
|---|---|
| Building our DAO | Governance and Treasury |
| Building our Game | The Metagame |
| Sophia working threads | Sophia and Server Ops |

### Channel renames

| Old | New |
|---|---|
| marketing | go-to-market |
| nft-planning | nft-and-memberships |
| game-design | metagame-design |

## Operating rules

- **Permissions:** the `sophia_truesight` bot is an **Administrator** (via the `Sophia TrueSight`
  managed role) — same authority as on Telegram, per `plans/DISCORD_ADAPTER_PLAN.md`.
  If ever tightened to least-privilege, **Manage Channels** is the single permission that
  covers creating/renaming/**deleting** both channels *and* categories ("folders").
- **Deletions:** `delete_discord_channel` requires `dry_run=false` **and** `confirm=true`.
  Prefer rename-and-move to an **Archive** category over deletion.
- **Adapter:** routing keys on **channel ID**, not name — renames/reorders are safe.
  New channels must be added to the adapter whitelist (`plans/DISCORD_ADAPTER_PLAN.md` §5).
- **Tool quirk:** the create/rename tool HTML-escapes `&`; use the word "and" in channel
  names, or fix the escaping in the Discord tooling.
