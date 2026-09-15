#!/bin/bash
# df-alert-fleet.sh — alert when a fleet host's root disk crosses thresholds.
# Runs from the autopilot box (has bot creds). Never auto-deletes. Read-only probe.
#
# Credentials: keys are resolved from the encrypted vault by scripts/fleet_probe.py
# (vault-first, on-box PEM fallback) — this script holds NO key path and NO host
# registry. Host ip/user/port/keys live in app/tools/ssh_tools.py::FLEET, so a host
# that moves is fixed in one place.
set -u
THRESH_ALERT=85
THRESH_CRIT=93
REPO=/opt/truesight_autopilot
PROBE="$REPO/.venv/bin/python3 $REPO/scripts/fleet_probe.py"
set -a; . "$REPO/.env" 2>/dev/null; set +a
TOKEN="${TELEGRAM_BOT_API_KEY:-}"; CHAT="${DEPLOY_NOTIFY_CHAT_ID:-}"
[ -z "$TOKEN" ] || [ -z "$CHAT" ] && { echo "$(date -Is) missing creds" >> /var/log/df-alert.log; exit 0; }
# Hosts to watch (FLEET labels in app/tools/ssh_tools.py).
HOSTS="krake_data krake_ror seni_ror dao_protocol"
for NAME in $HOSTS; do
  STAMP="/tmp/.df-alert-${NAME}-last"
  PCT=$($PROBE --host "$NAME" --command 'df / --output=pcent 2>/dev/null | tail -1 | tr -d " %"' --timeout 15 2>/dev/null)
  [ -z "${PCT:-}" ] && continue
  case "$PCT" in ''|*[!0-9]*) continue;; esac
  if [ -f "$STAMP" ]; then LAST=$(cat "$STAMP"); [ "$LAST" = "$PCT" ] && continue; fi
  [ "$PCT" -lt "$THRESH_ALERT" ] && continue
  echo "$PCT" > "$STAMP"
  LEVEL="ALERT"; [ "$PCT" -ge "$THRESH_CRIT" ] && LEVEL="CRITICAL"
  USED=$($PROBE --host "$NAME" --command 'df -h / | tail -1 | awk "{print \$3\" used, \"\$4\" avail\"}"' --timeout 15 2>/dev/null)
  MSG="[df-alert] $NAME root disk ${PCT}% (${LEVEL}) — / is at ${USED}."
  curl -s -m 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" -d chat_id="$CHAT" -d text="$MSG" > /dev/null 2>&1
  echo "$(date -Is) ${NAME} ${PCT}% ${LEVEL} notified" >> /var/log/df-alert.log
done
exit 0
