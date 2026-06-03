#!/bin/bash
# Git credential helper for the autopilot box.
#
# Feeds the TRUESIGHT_DAO_AUTOPILOT PAT from /opt/truesight_autopilot/.env to
# any git process on the host (manual ops, gas_deploy context pulls, etc.).
# Reading the .env at call time means PAT rotation never strands git — no
# stale ~/.git-credentials copy to chase.
#
# Installed by scripts/deploy.sh as the global credential.helper.
# The app-level git tool (app/tools/git_tools.py) carries its own inline
# helper and does not depend on this file.

[ "$1" = "get" ] || exit 0

ENV_FILE="${SOPHIA_ENV_FILE:-/opt/truesight_autopilot/.env}"
[ -f "$ENV_FILE" ] || exit 0

PAT=$(grep -E '^TRUESIGHT_DAO_AUTOPILOT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
[ -n "$PAT" ] || exit 0

echo "username=x-access-token"
echo "password=$PAT"
