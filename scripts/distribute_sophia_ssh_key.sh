#!/bin/bash
# Distribute Sophia's sophia_infra public key to the EC2 fleet.
#
# Run from the OPERATOR Mac (which already has fleet access via its own
# keys / ~/.ssh/config). Idempotent: appends the pubkey to each host's
# ~/.ssh/authorized_keys only if it isn't there yet.
#
# Host list mirrors app/tools/ssh_tools.py FLEET (which mirrors
# agentic_ai_context/AWS_DIGITAL_INFRASTRUCTURE.md §2). Update all three
# together when the fleet changes.
#
# Revocation: grep for the key comment on each host —
#   grep -v sophia-infra-truesight-autopilot ~/.ssh/authorized_keys

set -u

PUBKEY_FILE="${SOPHIA_PUBKEY:-$HOME/.ssh/sophia_infra.pub}"
if [ ! -f "$PUBKEY_FILE" ]; then
    echo "ERROR: $PUBKEY_FILE not found. Run scripts/deploy.sh once to generate the keypair."
    exit 1
fi
PUBKEY=$(cat "$PUBKEY_FILE")

# label:ip — keep in sync with app/tools/ssh_tools.py FLEET.
HOSTS="
krake_nginx:54.226.114.186
seni_ror:54.211.179.126
dao_protocol:98.93.94.86
seni_sk:34.234.193.80
seni_sql:44.193.55.205
seni_redis:54.234.59.188
krake_ror:18.205.20.43
krake_sk:54.227.147.20
krake_sk_webhook:52.207.88.236
krake_sk_crawler:52.91.57.12
krake_sk_scaler:100.25.41.96
krake_data:52.5.179.48
getdata_redis:52.1.162.134
getdata_cache:98.84.169.188
"

OK=()
FAIL=()
for entry in $HOSTS; do
    label="${entry%%:*}"
    ip="${entry##*:}"
    echo "--- $label ($ip) ---"
    if ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
        "ubuntu@$ip" \
        "grep -qF '$PUBKEY' ~/.ssh/authorized_keys 2>/dev/null \
            && echo 'already authorized' \
            || { echo '$PUBKEY' >> ~/.ssh/authorized_keys && echo 'key added'; }"; then
        OK+=("$label")
    else
        FAIL+=("$label")
        echo "  FAILED (no operator access from this machine?)"
    fi
done

echo
echo "=== Summary ==="
echo "authorized: ${OK[*]:-none}"
echo "failed:     ${FAIL[*]:-none}"
[ ${#FAIL[@]} -eq 0 ] || exit 1
