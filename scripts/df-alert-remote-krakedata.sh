#!/bin/bash
# df-alert-remote-krakedata.sh — krake_data-only disk alert (legacy narrow variant).
# Kept for continuity; df-alert-fleet.sh already covers krake_data. Never auto-deletes.
# Credentials resolved from the vault via scripts/fleet_probe.py — no key path here.
set -u
THRESH_ALERT=85
THRESH_CRIT=93
STAMP_FILE=/tmp/.df-alert-krakedata-last
REPO=/opt/truesight_autopilot
PROBE="$REPO/.venv/bin/python3 $REPO/scripts/fleet_probe.py"
PCT=$($PROBE --host krake_data --command 'df / --output=pcent 2>/dev/null | tail -1 | tr -d " %"' --timeout 15 2>/dev/null)
[ -z "${PCT:-}" ] && exit 0
case "$PCT" in ''|*[!0-9]*) exit 0;; esac
if [ -f "$STAMP_FILE" ]; then
  LAST=$(cat "$STAMP_FILE")
  [ "$LAST" = "$PCT" ] && exit 0
fi
[ "$PCT" -lt "$THRESH_ALERT" ] && exit 0
echo "$PCT" > "$STAMP_FILE"
set -a; . "$REPO/.env" 2>/dev/null; set +a
TOKEN="${TELEGRAM_BOT_API_KEY:-}"; CHAT="${DEPLOY_NOTIFY_CHAT_ID:-}"
[ -z "$TOKEN" ] || [ -z "$CHAT" ] && { echo "$(date -Is) missing creds" >> /var/log/df-alert.log; exit 0; }
LEVEL="ALERT"; [ "$PCT" -ge "$THRESH_CRIT" ] && LEVEL="CRITICAL"
USED=$($PROBE --host krake_data --command 'df -h / | tail -1 | awk "{print \$3\" used, \"\$4\" avail\"}"' --timeout 15 2>/dev/null)
MSG="[df-alert] krake_data root disk ${PCT}% (${LEVEL}) — / is at ${USED}."
curl -s -m 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" -d chat_id="$CHAT" -d text="$MSG" > /dev/null 2>&1
echo "$(date -Is) ${PCT}% ${LEVEL} notified" >> /var/log/df-alert.log
