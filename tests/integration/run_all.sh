#!/usr/bin/env bash
# Run all chat integration tests against a running autopilot.
#
# Usage:
#   tests/integration/run_all.sh              # against http://127.0.0.1:8011 (default)
#   AUTOPILOT_URL=http://example tests/integration/run_all.sh
#
# Assumes:
#   - A local autopilot is already running. Start one with:
#       python scripts/launch_local_autopilot.py
#   - dao_client/.env contains the governor's RSA key.
#
# Exits non-zero if any test fails.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

AUTOPILOT_URL="${AUTOPILOT_URL:-http://127.0.0.1:8011}"
export AUTOPILOT_URL

echo
echo "================================================================="
echo "  autopilot integration tests against ${AUTOPILOT_URL}"
echo "================================================================="

PYTHON_BIN="${PYTHON_BIN:-../../.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "FATAL: $PYTHON_BIN not executable. Run from repo root with venv activated."
    exit 2
fi

failed=0
for t in test_chat_smoke.py test_chat_interjection.py test_chat_cancel.py; do
    echo
    if ! "$PYTHON_BIN" "$t"; then
        echo "*** $t FAILED"
        failed=$((failed + 1))
    fi
done

echo
if [ "$failed" -eq 0 ]; then
    echo "ALL INTEGRATION TESTS PASSED"
    exit 0
fi
echo "FAILED: $failed integration test(s)"
exit 1
