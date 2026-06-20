#!/bin/bash
set -e

# Deploy truesight_autopilot to EC2
# Assumes governor_chatbot_service EC2 is already provisioned

EC2_HOST="${EC2_HOST:-sophia}"
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

echo "=== Pre-deploy syntax check ==="
SYNTAX_ERRORS=0
while IFS= read -r -d '' PYFILE; do
    if ! python -m py_compile "$PYFILE" 2>&1; then
        echo "SYNTAX ERROR in $PYFILE"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
    fi
done < <(find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' -print0)
if [ "$SYNTAX_ERRORS" -gt 0 ]; then
    echo "ABORTING DEPLOY: $SYNTAX_ERRORS Python file(s) have syntax errors."
    echo "The running service on EC2 is untouched."
    exit 1
fi
echo "All Python files pass syntax check."

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

# Sync .env — MERGE-ONLY (never overwrite existing prod values).
# The BOX .env is the source of truth for prod secrets — including Sophia's own
# identity (EMAIL / PRIVATE_KEY / PUBLIC_KEY), which intentionally DIFFERS from a
# developer's local .env. A wholesale `scp .env` would clobber that identity (and
# any other prod-only value) with the deployer's local copy. So we only ADD keys
# the box is MISSING and never touch an existing value. To change a value on the
# box, edit it on the box directly. (Set ENV_SYNC=skip to skip this entirely.)
if [ -f ".env" ] && [ "${ENV_SYNC:-merge}" != "skip" ]; then
    echo "=== Syncing .env (merge-only: add missing keys, never overwrite existing) ==="
    scp -i "$EC2_KEY" .env "$EC2_HOST:$REMOTE_DIR/.env.incoming"
    ssh -i "$EC2_KEY" "$EC2_HOST" 'bash -s' "$REMOTE_DIR" <<'REMOTE_MERGE'
set -e
BOX_ENV="$1/.env"; INC="$1/.env.incoming"
touch "$BOX_ENV"; added=0
while IFS= read -r line; do
    case "$line" in ''|\#*) continue;; esac
    key="${line%%=*}"
    if ! grep -q "^${key}=" "$BOX_ENV"; then
        printf '%s\n' "$line" >> "$BOX_ENV"; added=$((added+1)); echo "  + added $key"
    fi
done < "$INC"
rm -f "$INC"; chmod 600 "$BOX_ENV"
echo "merge complete: $added key(s) added; existing box values untouched"
REMOTE_MERGE
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

echo "=== Syncing sophia_infra SSH key (fleet access for ssh_run tool) ==="
# Dedicated outbound keypair for Sophia. Source of truth = operator Mac at
# ~/.ssh/sophia_infra (generated here if missing, so the same identity
# survives box rebuilds). Pubkey distribution to the fleet is a separate,
# operator-run step: scripts/distribute_sophia_ssh_key.sh.
if [ ! -f "$HOME/.ssh/sophia_infra" ]; then
    ssh-keygen -t ed25519 -N "" -C "sophia-infra-truesight-autopilot" \
        -f "$HOME/.ssh/sophia_infra"
    echo "  -> generated new sophia_infra keypair on operator machine"
    echo "  -> REMEMBER: run scripts/distribute_sophia_ssh_key.sh to authorize it on the fleet"
fi
scp -i "$EC2_KEY" -q "$HOME/.ssh/sophia_infra" "$HOME/.ssh/sophia_infra.pub" "$EC2_HOST:~/.ssh/"
ssh -i "$EC2_KEY" "$EC2_HOST" "chmod 600 ~/.ssh/sophia_infra && chmod 644 ~/.ssh/sophia_infra.pub && echo 'sophia_infra key synced'"

# Self-trust: authorize sophia_infra.pub on this box's OWN authorized_keys so
# ssh_run(host='autopilot', ...) (loopback 127.0.0.1) works — Sophia's path to
# running sudo / installing packages on the machine she runs on. Idempotent.
ssh -i "$EC2_KEY" "$EC2_HOST" "
    touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
    PUB=\$(cat ~/.ssh/sophia_infra.pub)
    grep -qF \"\$PUB\" ~/.ssh/authorized_keys || echo \"\$PUB\" >> ~/.ssh/authorized_keys
    echo 'sophia_infra self-trust ensured (loopback self-exec enabled)'
"

echo "=== Installing Node 20 + clasp (GAS deploys) ==="
# gas_deploy_project tool needs node + clasp on PATH (see its docstring).
# NodeSource Node 20 matches the operator Mac (nvm v20.19.1); clasp pinned
# to 3.3.0 for the same reason. Both steps are idempotent.
ssh -i "$EC2_KEY" "$EC2_HOST" "
    if ! command -v node &>/dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
        echo 'node installed:' \$(node --version)
    else
        echo 'node already installed:' \$(node --version)
    fi
    if ! command -v clasp &>/dev/null; then
        sudo npm install -g @google/clasp@3.3.0
        echo 'clasp installed:' \$(clasp --version)
    else
        echo 'clasp already installed:' \$(clasp --version)
    fi
"

echo "=== Syncing clasp credentials (~/.clasprc.json) ==="
# clasp login is interactive OAuth — the token file is the portable artifact.
# Source of truth is the operator Mac's ~/.clasprc.json (admin clasp login).
if [ -f "$HOME/.clasprc.json" ]; then
    scp -i "$EC2_KEY" -q "$HOME/.clasprc.json" "$EC2_HOST:~/.clasprc.json"
    ssh -i "$EC2_KEY" "$EC2_HOST" "chmod 600 ~/.clasprc.json && echo 'clasprc synced'"
else
    echo "  WARN: ~/.clasprc.json missing locally — run 'clasp login' first."
    echo "  Without it, gas_deploy_project pushes will fail as unauthenticated."
fi

echo "=== Provisioning git identity + credential helper ==="
# Sophia's native git capability (app/tools/git_tools.py + manual ops).
# The helper reads the PAT from .env at call time — PAT rotation safe.
ssh -i "$EC2_KEY" "$EC2_HOST" "
    chmod +x $REMOTE_DIR/scripts/git-credential-sophia.sh
    git config --global user.name 'Sophia (TrueSight Autopilot)'
    git config --global user.email 'sophia@truesight.me'
    git config --global credential.helper '$REMOTE_DIR/scripts/git-credential-sophia.sh'
    git config --global init.defaultBranch main
    echo 'git identity + credential helper configured'
"

echo "=== Installing tesseract-ocr (attachment processing) ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "DEBIAN_FRONTEND=noninteractive apt-get install -y tesseract-ocr"

echo "=== Installing deps on EC2 ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    cd $REMOTE_DIR

    if [ ! -d .venv ]; then
        echo 'No .venv found — creating fresh'
        python3 -m venv .venv
    fi

    REQ_HASH=\$(sha256sum requirements.txt | cut -d' ' -f1)
    LAST_HASH=\$(cat .req-hash 2>/dev/null || echo '')
    if [ \"\$REQ_HASH\" != \"\$LAST_HASH\" ]; then
        echo 'requirements.txt changed — reinstalling deps'
        source .venv/bin/activate
        pip install -r requirements.txt
        pip install 'truesight-dao-client @ git+https://github.com/TrueSightDAO/dao_client.git'
        echo \"\$REQ_HASH\" > .req-hash
        echo 'deps installed + hash updated'
    else
        echo 'requirements.txt unchanged — skipping pip install'
    fi
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
    sudo cp $REMOTE_DIR/systemd/truesight-autopilot-watchdog.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable truesight-autopilot truesight-autopilot-telegram
    echo \"Waiting 5s for active requests to drain before restart...\"
    sleep 5
    sudo systemctl restart truesight-autopilot
    # Vault worker (port 8002) — restart so credential-vault fixes actually take
    # effect; it was silently running stale code because deploys skipped it.
    sudo systemctl restart truesight-vault 2>/dev/null || true
    # Start the Telegram adapter only when a bot token is configured.
    if grep -q '^TELEGRAM_BOT_API_KEY=.' $REMOTE_DIR/.env 2>/dev/null; then
        sudo systemctl restart truesight-autopilot-telegram
    else
        echo 'TELEGRAM_BOT_API_KEY not set in .env — leaving telegram adapter stopped.'
        sudo systemctl stop truesight-autopilot-telegram 2>/dev/null || true
    fi
    # Start the attention watchdog only once its user-session exists
    # (one-time interactive: .venv/bin/python scripts/telethon_login.py).
    if [ -f $REMOTE_DIR/.telethon_watchdog.session ]; then
        sudo systemctl enable truesight-autopilot-watchdog
        sudo systemctl restart truesight-autopilot-watchdog
    else
        echo 'No .telethon_watchdog.session — run scripts/telethon_login.py once; leaving watchdog stopped.'
        sudo systemctl stop truesight-autopilot-watchdog 2>/dev/null || true
    fi
    sleep 2
    sudo systemctl status truesight-autopilot --no-pager
    sudo systemctl status truesight-autopilot-telegram --no-pager || true
    sudo systemctl status truesight-autopilot-watchdog --no-pager || true
"

echo "=== Setting up nginx + certbot ==="
ssh -i "$EC2_KEY" "$EC2_HOST" "
    # Install the http-context zone file FIRST (conf.d gets included in http
    # context). The server block depends on the zone existing. Always reinstall
    # the symlinks — they're idempotent — and re-test/reload nginx so a half-
    # installed previous deploy recovers cleanly.
    sudo ln -sf $REMOTE_DIR/config/nginx/sophia-zones.conf /etc/nginx/conf.d/sophia-zones.conf
    sudo ln -sf $REMOTE_DIR/config/nginx/sophia.conf /etc/nginx/sites-available/sophia
    sudo ln -sf /etc/nginx/sites-available/sophia /etc/nginx/sites-enabled/
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    echo 'nginx configured for sophia.truesight.me'

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
