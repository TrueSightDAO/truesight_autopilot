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

# Use git instead of rsync so the EC2 has a proper repo with version history
# This enables autopilot to self-deploy via git pull + restart
ssh -i "$EC2_KEY" "$EC2_HOST" "
    if [ -d $REMOTE_DIR/.git ]; then
        echo 'Git repo exists — pulling latest...'
        cd $REMOTE_DIR && git fetch origin main && git reset --hard origin/main && git clean -fd
    else
        echo 'First deploy or migration from rsync — initializing git repo...'
        # Preserve .env if it exists (will be re-synced below anyway)
        if [ -f $REMOTE_DIR/.env ]; then
            cp $REMOTE_DIR/.env /tmp/truesight_autopilot_env_backup
        fi
        # Remove all old rsynced files
        rm -rf $REMOTE_DIR/*
        rm -rf $REMOTE_DIR/.*  2>/dev/null || true
        # Init git repo and pull from origin
        cd $REMOTE_DIR
        git init
        git remote add origin https://github.com/TrueSightDAO/truesight_autopilot.git
        git fetch --depth 1 origin main
        git checkout -B main origin/main
        if [ -f /tmp/truesight_autopilot_env_backup ]; then
            mv /tmp/truesight_autopilot_env_backup $REMOTE_DIR/.env
        fi
    fi
"

# Sync .env separately (gitignored locally, needed on EC2)
if [ -f ".env" ]; then
    echo "=== Checking .env key parity (local vs production) ==="
    LOCAL_KEYS=$(grep -v '^#' .env | grep -v '^$' | cut -d= -f1 | sort)
    REMOTE_KEYS=$(ssh -i "$EC2_KEY" "$EC2_HOST" \
        "grep -v '^#' $REMOTE_DIR/.env 2>/dev/null | grep -v '^$' | cut -d= -f1 | sort" 2>/dev/null || echo "")
    if [ -n "$REMOTE_KEYS" ]; then
        MISSING_FROM_LOCAL=$(comm -13 <(echo "$LOCAL_KEYS") <(echo "$REMOTE_KEYS"))
        MISSING_FROM_PROD=$(comm -23 <(echo "$LOCAL_KEYS") <(echo "$REMOTE_KEYS"))
        if [ -n "$MISSING_FROM_LOCAL" ]; then
            echo "WARNING: Keys in production .env but NOT in local .env:"
            echo "$MISSING_FROM_LOCAL" | sed 's/^/  - /'
            echo "These will be LOST when .env is synced. Add them to local .env first."
            if [ "${SKIP_KEY_CHECK:-0}" != "1" ]; then
                echo "Aborting. Set SKIP_KEY_CHECK=1 to bypass."
                exit 1
            fi
        fi
        if [ -n "$MISSING_FROM_PROD" ]; then
            echo "Keys in local .env but NOT in production .env:"
            echo "$MISSING_FROM_PROD" | sed 's/^/  + /'
            echo "These will be ADDED to production."
        fi
    fi
    echo "=== Syncing .env ==="
    scp -i "$EC2_KEY" .env "$EC2_HOST:$REMOTE_DIR/.env"
fi

echo "=== Syncing Gmail OAuth tokens (config/gmail/*.json) ==="
if [ -d "config/gmail" ]; then
    GMAIL_JSON_COUNT=$(ls config/gmail/*.json 2>/dev/null | wc -l | tr -d ' ')
    if [ "$GMAIL_JSON_COUNT" -gt 0 ]; then
        ssh -i "$EC2_KEY" "$EC2_HOST" "mkdir -p $REMOTE_DIR/config/gmail && chmod 700 $REMOTE_DIR/config/gmail"
        scp -i "$EC2_KEY" -q config/gmail/*.json "$EC2_HOST:$REMOTE_DIR/config/gmail/"
        ssh -i "$EC2_KEY" "$EC2_HOST" "chmod 600 $REMOTE_DIR/config/gmail/*.json && ls -la $REMOTE_DIR/config/gmail/"
        echo "  -> synced $GMAIL_JSON_COUNT Gmail token file(s)"
    else
        echo "  WARN: config/gmail/ exists but no .json files found. Gmail tools will return 'credentials missing' on EC2."
    fi
else
    echo "  WARN: config/gmail/ directory missing locally. Skipping. See config/gmail/README.md."
fi

echo "=== Syncing Google service-account credentials (config/google/*.json) ==="
# Gitignored binary creds — provisioned out-of-band on each developer machine;
# rsynced to EC2 with mode 600. See config/google/README.md.
if [ -d "config/google" ]; then
    GOOGLE_JSON_COUNT=$(ls config/google/*.json 2>/dev/null | wc -l | tr -d ' ')
    if [ "$GOOGLE_JSON_COUNT" -gt 0 ]; then
        ssh -i "$EC2_KEY" "$EC2_HOST" "mkdir -p $REMOTE_DIR/config/google && chmod 700 $REMOTE_DIR/config/google"
        scp -i "$EC2_KEY" -q config/google/*.json "$EC2_HOST:$REMOTE_DIR/config/google/"
        ssh -i "$EC2_KEY" "$EC2_HOST" "chmod 600 $REMOTE_DIR/config/google/*.json && ls -la $REMOTE_DIR/config/google/"
        echo "  -> synced $GOOGLE_JSON_COUNT credential file(s)"
    else
        echo "  WARN: config/google/ exists but no .json files found. Drive/Sheets tools will return 'credentials missing' on EC2."
    fi
else
    echo "  WARN: config/google/ directory missing locally. Skipping. See config/google/README.md for provisioning."
fi

echo "=== Syncing agentic_ai_context ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    mkdir -p $REMOTE_DIR/context
    if [ -d $REMOTE_DIR/context/agentic_ai_context/.git ]; then
        cd $REMOTE_DIR/context/agentic_ai_context && git fetch --all && git reset --hard origin/main && git clean -fd
    else
        rm -rf $REMOTE_DIR/context/agentic_ai_context
        git clone --depth 1 https://github.com/TrueSightDAO/agentic_ai_context.git $REMOTE_DIR/context/agentic_ai_context
    fi
"

echo "=== Syncing tokenomics (needed by gas_deploy_project tool) ==="
# Shallow clone of tokenomics so the autopilot can run
# scripts/deploy_gas_project.py for GAS deploys driven from Telegram.
# clasp itself is NOT installed here automatically — operator step (see
# app/tools/gas_deploy_project.py docstring).
ssh -i "$EC2_KEY" "$EC2_HOST" "
    if [ -d $REMOTE_DIR/context/tokenomics/.git ]; then
        cd $REMOTE_DIR/context/tokenomics && git fetch --all && git reset --hard origin/main && git clean -fd
    else
        rm -rf $REMOTE_DIR/context/tokenomics
        git clone --depth 1 https://github.com/TrueSightDAO/tokenomics.git $REMOTE_DIR/context/tokenomics
    fi
"

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

echo "=== Installing systemd services ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    sudo cp $REMOTE_DIR/systemd/truesight-autopilot.service /etc/systemd/system/
    sudo cp $REMOTE_DIR/systemd/truesight-autopilot-telegram.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable truesight-autopilot truesight-autopilot-telegram
    echo \"Waiting 5s for active requests to drain before restart...\"
    sleep 5
    sudo systemctl restart truesight-autopilot
    # Start the Telegram adapter only when a bot token is configured.
    if grep -q '^TELEGRAM_BOT_API_KEY=.' $REMOTE_DIR/.env 2>/dev/null; then
        sudo systemctl restart truesight-autopilot-telegram
    else
        echo 'TELEGRAM_BOT_API_KEY not set in .env — leaving telegram adapter stopped.'
        sudo systemctl stop truesight-autopilot-telegram 2>/dev/null || true
    fi
    sleep 2
    sudo systemctl status truesight-autopilot --no-pager
    sudo systemctl status truesight-autopilot-telegram --no-pager || true
"

echo "=== Setting up nginx + certbot ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    # Symlink nginx config
    if [ ! -L /etc/nginx/sites-enabled/sophia ]; then
        sudo ln -sf $REMOTE_DIR/config/nginx/sophia.conf /etc/nginx/sites-available/sophia
        sudo ln -sf /etc/nginx/sites-available/sophia /etc/nginx/sites-enabled/
        sudo rm -f /etc/nginx/sites-enabled/default
        sudo nginx -t && sudo systemctl reload nginx
        echo 'nginx configured for sophia.truesight.me'
    else
        echo 'nginx already configured for sophia.truesight.me'
    fi

    # Install certbot if not present
    if ! command -v certbot &>/dev/null; then
        sudo snap install --classic certbot
        sudo ln -sf /snap/bin/certbot /usr/bin/certbot
        echo 'certbot installed'
    else
        echo 'certbot already installed'
    fi

    # Get SSL cert (idempotent — certbot skips if already obtained)
    sudo certbot --nginx -d sophia.truesight.me --non-interactive --agree-tos -m garyjob@gmail.com || true
"

echo "=== Checking health ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "curl -s http://localhost:8001/health | python3 -m json.tool"

echo "=== Deploy complete ==="
