#!/bin/bash
set -e

# Start truesight_autopilot locally for development
# Usage: ./start_local.sh [--dry-run]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== truesight_autopilot local dev server ==="

# Check .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  .env not found. Copying from .env.example..."
    cp .env.example .env
    echo "   Please edit .env and fill in your credentials, then re-run."
    exit 1
fi

# Check required vars are non-empty
_check_env() {
    local key="$1"
    local val
    val=$(grep "^${key}=" .env | sed "s/^${key}=//" | head -1)
    if [ -z "$val" ]; then
        echo "⚠️  ${key} is empty in .env"
        return 1
    fi
    return 0
}

missing=0
_check_env "TRUESIGHT_DAO_AUTOPILOT" || missing=1
_check_env "DEEPSEEK_API_KEY" || _check_env "DEEPSEEK_SDK" || missing=1
_check_env "GMAIL_TOKEN_JSON" || missing=1

if [ "$missing" -eq 1 ]; then
    echo ""
    echo "Some required credentials are missing. The server may fail to start."
    echo "Edit .env and fill in: TRUESIGHT_DAO_AUTOPILOT, DEEPSEEK_API_KEY (or DEEPSEEK_SDK), GMAIL_TOKEN_JSON"
    echo ""
fi

# Create venv if needed
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate venv
source .venv/bin/activate

# Install deps if needed
if [ ! -f ".venv/.requirements_installed" ] || [ "requirements.txt" -nt ".venv/.requirements_installed" ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt -q
    touch .venv/.requirements_installed
fi

# Handle args
EXTRA=""
if [ "${1:-}" = "--dry-run" ]; then
    echo "🧪 DRY_RUN mode — no background tasks, no sheet writes"
    export DRY_RUN=true
    EXTRA="--dry-run"
fi

echo "Starting server on http://localhost:8001"
echo "Health check: curl http://localhost:8001/health"
echo "Chat endpoint: POST http://localhost:8001/chat"
echo "Press Ctrl+C to stop"
echo ""

# Run with auto-reload for dev
python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8001 \
    --reload \
    --reload-dir app
