#!/bin/bash
# df-alert.sh — alert (never auto-deletes) when THIS box's root disk crosses thresholds.
# Sourced from cron; posts to Telegram via the bot token in the autopilot .env.
# Local probe only — no SSH, no credentials. See df-alert-fleet.sh for remote hosts.
set -u
THRESH_ALERT=85
THRESH_CRIT=93
STAMP_FILE=/tmp/.df-alert-last
PCT=$(df / --output=pcent | tail -1 | tr -d ' %')
[ -z "${PCT:-}" ] && exit 0
# Cooldown: alert at most once per 12h at the SAME level to avoid spam.
if [ -f "$STAMP_FILE" ]; then
  LAST=$(cat "$STAMP_FILE")
  [ "$LAST" = "$PCT" ] && exit 0
fi
[ "$PCT" -lt "$THRESH_ALERT" ] && exit 0
echo "$PCT" > "$STAMP_FILE"
# Load bot creds without echoing them.
set -a; . /opt/truesight_autopilot/.env 2>/dev/null; set +a
TOKEN="${TELEGRAM_BOT_API_KEY:-}"
CHAT="${DEPLOY_NOTIFY_CHAT_ID:-}"
[ -z "$TOKEN" ] || [ -z "$CHAT" ] && { echo "df-alert: missing TELEGRAM_BOT_API_KEY/DEPLOY_NOTIFY_CHAT_ID" >> /var/log/df-alert.log; exit 0; }
LEVEL="ALERT"; [ "$PCT" -ge "$THRESH_CRIT" ] && LEVEL="CRITICAL"
MSG="[df-alert] autopilot root disk ${PCT}% (${LEVEL}) — / is at $(df -h / | tail -1 | awk '{print $3" used, "$4" avail"}')."
curl -s -m 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="$CHAT" -d text="$MSG" > /dev/null 2>&1
echo "$(date -Is) ${PCT}% ${LEVEL} notified" >> /var/log/df-alert.log
