#!/bin/bash
set -e

# Deploy truesight_autopilot to EC2
# Assumes governor_chatbot_service EC2 is already provisioned

EC2_HOST="${EC2_HOST:-truesight-autopilot}"
EC2_KEY="${EC2_KEY:-~/.ssh/agentic_ai_github/id_ed25519}"
REMOTE_DIR="/opt/truesight_autopilot"

echo "=== Building local repo ==="
cd "$(dirname "$0")/.."

# Ensure venv exists locally
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt

echo "=== Syncing to EC2 ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown ubuntu:ubuntu $REMOTE_DIR"

# rsync code (excluding venv, .env, .git)
rsync -avz --delete \
    --exclude='.venv' \
    --exclude='.env' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    ./ "$EC2_HOST:$REMOTE_DIR/"

echo "=== Installing deps on EC2 ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    cd $REMOTE_DIR
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
"

echo "=== Installing systemd service ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    sudo cp $REMOTE_DIR/systemd/truesight-autopilot.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable truesight-autopilot
    sudo systemctl restart truesight-autopilot
    sleep 2
    sudo systemctl status truesight-autopilot --no-pager
"

echo "=== Checking health ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "curl -s http://localhost:8001/health | python3 -m json.tool"

echo "=== Deploy complete ==="
