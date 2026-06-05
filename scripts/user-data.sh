#!/bin/bash
# Cloud-init user-data for truesight-autopilot EC2
# Runs on first boot

set -e

# Update system
apt-get update -y
apt-get upgrade -y

# Install Python 3.11 + deps
apt-get install -y python3.11 python3.11-venv python3-pip git nginx ffmpeg

# Node 20 + clasp for GAS deploys (gas_deploy_project tool). The clasp OAuth
# token (~/.clasprc.json) is synced later by scripts/deploy.sh.
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @google/clasp@3.3.0

# Create app directory
mkdir -p /opt/truesight_autopilot
chown ubuntu:ubuntu /opt/truesight_autopilot

# Install SSH public key for Gary
mkdir -p /home/ubuntu/.ssh
chown ubuntu:ubuntu /home/ubuntu/.ssh
chmod 700 /home/ubuntu/.ssh

cat >> /home/ubuntu/.ssh/authorized_keys << 'PUBKEY'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILVeXXbMiSGb3c0TQCmhzb7deVdm+De29bxCLHTsVc/m agentic-ai-github-TrueSightDAO
PUBKEY

chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys

# Allow passwordless sudo for ubuntu (convenience)
echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/99-ubuntu
chmod 440 /etc/sudoers.d/99-ubuntu

# Install CloudWatch agent for monitoring
wget -q https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
dpkg -i amazon-cloudwatch-agent.deb
rm amazon-cloudwatch-agent.deb

# Start CloudWatch agent with basic config
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c ssm:AmazonCloudWatch-linux

echo "Setup complete"
