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

# rsync code (excluding venv, .git, caches)
rsync -avz --delete \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    ./ "$EC2_HOST:$REMOTE_DIR/"

# Sync .env separately (gitignored locally, needed on EC2)
if [ -f ".env" ]; then
    echo "=== Syncing .env ==="
    scp -i "$EC2_KEY" .env "$EC2_HOST:$REMOTE_DIR/.env"
fi

echo "=== Installing deps on EC2 ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    cd $REMOTE_DIR
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    # Ensure dao_client is installed from GitHub (needed for Edgar submissions)
    pip install 'truesight-dao-client @ git+https://github.com/TrueSightDAO/dao_client.git'
"

echo "=== Installing helper scripts ==="
cat > /tmp/truesight-autopilot-logs.sh << 'SCRIPT'
#!/bin/bash
# Monitor truesight-autopilot logs
CMD="${1:-follow}"
case "$CMD" in
    follow|-f|f)
        sudo journalctl -u truesight-autopilot -f --no-hostname | cut -d' ' -f4-
        ;;
    tail|t|last)
        COUNT="${2:-50}"
        sudo journalctl -u truesight-autopilot --no-pager -n "$COUNT" --no-hostname | cut -d' ' -f4-
        ;;
    today)
        sudo journalctl -u truesight-autopilot --since today --no-hostname | cut -d' ' -f4-
        ;;
    errors|e)
        sudo journalctl -u truesight-autopilot -p err --no-pager -n 50 --no-hostname | cut -d' ' -f4-
        ;;
    health)
        curl -s http://localhost:8001/health | python3 -m json.tool
        ;;
    *)
        echo "Usage: $0 {follow|tail [N]|today|errors|health}"
        exit 1
        ;;
esac
SCRIPT
scp -i "$EC2_KEY" /tmp/truesight-autopilot-logs.sh "$EC2_HOST:~/truesight-autopilot-logs.sh"
ssh -i "$EC2_KEY" "$EC2_HOST" "chmod +x ~/truesight-autopilot-logs.sh"
rm /tmp/truesight-autopilot-logs.sh

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
