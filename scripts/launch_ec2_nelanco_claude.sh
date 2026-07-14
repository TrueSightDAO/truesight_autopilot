#!/bin/bash
# Launch the NELANCO CLAUDE CODE BOX.
#
# An interactive Claude Code jump-box in the Nelanco AWS account (767697632458),
# with Sophia-parity environment + full fleet SSH. Driven from the Claude mobile
# app via `claude --remote-control`. Design + gates:
#   agentic_ai_context/plans/NELANCO_CLAUDE_CODE_BOX_PLAN.md
#
# THIS IS AN ALWAYS-STOP (Gate B) STEP: running it provisions live infra. Merging
# the script does nothing. Review the CONFIRM markers below before running.
#
# After this succeeds, do Gate C (laydown: creds + repo clones + fleet SSH key +
# ~/.ssh/config alias + `claude` login) and add this box's IP to the Nelanco
# fleet SG allowlist so it can SSH the fleet.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# CONFIRM these against the live Nelanco account before running (Gate B).
# Nelanco values differ from Sophia's Explorya box — do not copy Explorya's.
# ---------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-us-east-1}"
EXPECTED_ACCOUNT="767697632458"                 # Nelanco
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"     # D4
KEY_NAME="${KEY_NAME:-GETDATA_IO_PAIR_20201122}"  # Nelanco keypair (NELANCO_aws_20201122.pem) — CONFIRM
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"      # CONFIRM: a Nelanco SG allowing SSH(22) from your IP
SUBNET_ID="${SUBNET_ID:-}"                       # CONFIRM: a Nelanco subnet (same VPC as fleet is convenient)
VOLUME_GB="${VOLUME_GB:-30}"
NAME_TAG="${NAME_TAG:-nelanco-claude-code}"

if [ -z "$SECURITY_GROUP_ID" ] || [ -z "$SUBNET_ID" ]; then
    echo "ERROR: set SECURITY_GROUP_ID and SUBNET_ID (Nelanco values) before running."
    echo "  e.g. SECURITY_GROUP_ID=sg-xxxx SUBNET_ID=subnet-xxxx $0"
    exit 1
fi

# ---------------------------------------------------------------------------
# AWS credentials for the Nelanco account. Prefers explicit *_NELANCO env, then
# the autopilot .env, then cypher_def/.env. Must have ec2:RunInstances etc. in
# Nelanco (the 2026 SG-remediation was blocked on a write IAM key — verify).
# ---------------------------------------------------------------------------
REPO_DIR="$(dirname "$SCRIPT_DIR")"
AUTOPILOT_ENV="$REPO_DIR/.env"
CYPHER_ENV="$REPO_DIR/../cypher_def/.env"
getkey() { grep "^$1=" "$2" 2>/dev/null | head -1 | sed "s/^$1=//"; }

if [ -n "${AWS_ACCESS_KEY_ID_NELANCO:-}" ]; then
    export AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID_NELANCO"
    export AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY_NELANCO"
elif [ -f "$AUTOPILOT_ENV" ] && [ -n "$(getkey AWS_ACCESS_KEY_ID_NELANCO "$AUTOPILOT_ENV")" ]; then
    export AWS_ACCESS_KEY_ID="$(getkey AWS_ACCESS_KEY_ID_NELANCO "$AUTOPILOT_ENV")"
    export AWS_SECRET_ACCESS_KEY="$(getkey AWS_SECRET_ACCESS_KEY_NELANCO "$AUTOPILOT_ENV")"
elif [ -f "$CYPHER_ENV" ]; then
    export AWS_ACCESS_KEY_ID="$(getkey TRUESIGHT_DAO_AUTOPILOT_AWS_KEY "$CYPHER_ENV")"
    export AWS_SECRET_ACCESS_KEY="$(getkey TRUESIGHT_DAO_AUTOPILOT_AWS_SECRET "$CYPHER_ENV")"
else
    echo "ERROR: no Nelanco AWS credentials found (env, $AUTOPILOT_ENV, or $CYPHER_ENV)."
    exit 1
fi
export AWS_REGION

echo "=== Verifying AWS identity (expect account $EXPECTED_ACCOUNT / Nelanco) ==="
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "  authenticated to account: $ACCOUNT"
if [ "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
    echo "ERROR: wrong account. Expected Nelanco $EXPECTED_ACCOUNT, got $ACCOUNT. Aborting."
    exit 1
fi

# Resolve the latest Ubuntu 22.04 LTS AMI via Canonical's public SSM parameter
# (region-correct, no hardcoded stale AMI id).
echo "=== Resolving latest Ubuntu 22.04 AMI in $AWS_REGION ==="
AMI_ID=$(aws ssm get-parameters \
  --names /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
  --query 'Parameters[0].Value' --output text)
echo "  AMI: $AMI_ID"

echo "=== Launching EC2 ($INSTANCE_TYPE, ${VOLUME_GB}GB encrypted gp3) ==="
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SECURITY_GROUP_ID" \
  --subnet-id "$SUBNET_ID" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_TAG},{Key=Project,Value=TrueSightDAO},{Key=Service,Value=claude-code}]" \
  --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$VOLUME_GB,\"VolumeType\":\"gp3\",\"Encrypted\":true}}]" \
  --user-data "file://$SCRIPT_DIR/user-data-claude.sh" \
  --query 'Instances[0].InstanceId' --output text)
echo "  instance: $INSTANCE_ID"

echo "=== Waiting for instance-running ==="
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# Stable IP: allocate + associate an Elastic IP (blue-green friendly; also the IP
# you add to the Nelanco fleet SG allowlist and to ~/.ssh/config).
echo "=== Allocating + associating Elastic IP ==="
ALLOC_ID=$(aws ec2 allocate-address --domain vpc \
  --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME_TAG}]" \
  --query AllocationId --output text)
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
PUBLIC_IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" \
  --query 'Addresses[0].PublicIp' --output text)
echo "  EIP: $PUBLIC_IP  (alloc $ALLOC_ID)"

# Point the ~/.ssh/config alias at the new EIP if a placeholder exists (Gate C
# adds the alias if it isn't there yet).
if grep -q "HostName PLACEHOLDER_NELANCO_CLAUDE_IP" ~/.ssh/config 2>/dev/null; then
    sed -i.bak "s/HostName PLACEHOLDER_NELANCO_CLAUDE_IP/HostName $PUBLIC_IP/" ~/.ssh/config
    echo "  updated ~/.ssh/config (backup ~/.ssh/config.bak)"
fi

echo "=== Waiting for SSH ==="
for i in {1..30}; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i ~/.ssh/agentic_ai_github/id_ed25519 "ubuntu@$PUBLIC_IP" "echo ok" 2>/dev/null; then
        break
    fi
    echo "  waiting for SSH... ($i/30)"; sleep 5
done

cat <<EOF

=== Provisioned ===
  instance : $INSTANCE_ID
  EIP      : $PUBLIC_IP
  ssh      : ssh -i ~/.ssh/agentic_ai_github/id_ed25519 ubuntu@$PUBLIC_IP

Next (see the plan):
  Gate B  : add $PUBLIC_IP to the Nelanco fleet SG allowlist (for fleet SSH parity)
  Gate C  : run the laydown (creds + repo clones + dedicated fleet key), add the
            'nelanco-claude' Host alias to ~/.ssh/config, and 'claude' login.
Run mode : ssh nelanco-claude -> tmux -> claude -> /remote-control -> drive from phone
EOF
