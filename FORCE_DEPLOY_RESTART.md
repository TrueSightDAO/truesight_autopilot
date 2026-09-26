# Force deploy restart

This file is a *request* flag: bumping it schedules a redeploy so the box
restarts onto the latest `main` and loads new code (Python does not hot-reload).
It does NOT bypass the idle-drain guard — the actual restart runs via
`deploy_autopilot` / `deploy.sh` with `can_deploy(force=False)`, so in-flight
turns drain first. Delete after deploy.

Scheduled redeploy 2026-06-17 to pick up:
- Live-progress cross-process fix (#241) — mid-turn ack + /chat/progress now
  surface the running-turn snapshot.
- Auto-advance feature (#244 parser, #248 brain signal, #246 adapter loop) —
  shipped behind the `AUTO_ADVANCE` flag (default OFF; no behavior change until
  flipped + UAT).

Timestamp: 2026-06-17T23:25:21Z
# Force deploy trigger 1781738721
