#!/bin/bash
set -e

# Launch a new EC2 for truesight_autopilot
# Run this AFTER AWS unblocks account 767697632458

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Load AWS creds from cypher_def/.env
CYPHER_ENV="$REPO_DIR/../cypher_def/.env"
if [ ! -f "$CYPHER_ENV" ]; then
    echo "Error: $CYPHER_ENV not found"
    exit 1
fi

export AWS_ACCESS_KEY_ID="$(grep '^TRUESIGHT_DAO_AUTOPILOT_AWS_KEY=' "$CYPHER_ENV" | sed 's/^TRUESIGHT_DAO_AUTOPILOT_AWS_KEY=//')"
export AWS_SECRET_ACCESS_KEY="$(grep '^TRUESIGHT_DAO_AUTOPILOT_AWS_SECRET=' "$CYPHER_ENV" | sed 's/^TRUESIGHT_DAO_AUTOPILOT_AWS_SECRET=//')"
export AWS_REGION="us-east-1"

echo "=== Verifying AWS credentials ==="
aws sts get-caller-identity

echo ""
echo "=== Launching EC2 ==="
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-00403f401ee6a4b98 \
  --instance-type t3.small \
  --key-name garyjob_aws \
  --security-group-ids sg-e98f788e \
  --subnet-id subnet-44257d33 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=truesight-autopilot},{Key=Project,Value=TrueSightDAO},{Key=Service,Value=autopilot}]' \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --user-data file://"$SCRIPT_DIR/user-data.sh" \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance ID: $INSTANCE_ID"

echo ""
echo "=== Waiting for instance to be running ==="
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

echo ""
echo "=== Getting public IP ==="
PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

echo "Public IP: $PUBLIC_IP"

echo ""
echo "=== Updating ~/.ssh/config ==="
# Update the HostName in ~/.ssh/config
sed -i.bak "s/HostName PLACEHOLDER_IP/HostName $PUBLIC_IP/" ~/.ssh/config
echo "Updated ~/.ssh/config (backup saved as ~/.ssh/config.bak)"

echo ""
echo "=== Waiting for SSH to be ready ==="
for i in {1..30}; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i ~/.ssh/agentic_ai_github/id_ed25519 "ubuntu@$PUBLIC_IP" "echo 'SSH ready'" 2>/dev/null; then
        break
    fi
    echo "  Waiting for SSH... ($i/30)"
    sleep 5
done

echo ""
echo "=== Running deploy ==="
export EC2_HOST="ubuntu@$PUBLIC_IP"
export EC2_KEY="~/.ssh/agentic_ai_github/id_ed25519"
"$SCRIPT_DIR/deploy.sh"

echo ""
echo "=== Done ==="
echo "EC2: $INSTANCE_ID @ $PUBLIC_IP"
echo "SSH: ssh truesight-autopilot"
echo "Health: curl http://$PUBLIC_IP:8001/health"
