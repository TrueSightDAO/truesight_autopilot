#!/bin/bash
# Generate an ed25519 SSH key for the autopilot to use with GitHub and EC2.
# Usage: bash scripts/generate_ssh_key.sh
# Outputs the public key to stdout.

set -e

KEY_PATH="$HOME/.ssh/id_ed25519_truesight_autopilot"

if [ -f "$KEY_PATH" ]; then
  echo "SSH key already exists at $KEY_PATH" >&2
  cat "${KEY_PATH}.pub"
  exit 0
fi

ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "truesight-autopilot-$(hostname)" >&2
chmod 600 "$KEY_PATH"
chmod 644 "${KEY_PATH}.pub"

# Configure ~/.ssh/config for github.com
SSH_CONFIG="$HOME/.ssh/config"
if ! grep -q "Host github.com" "$SSH_CONFIG" 2>/dev/null; then
  cat >> "$SSH_CONFIG" <<'CONF'

Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_truesight_autopilot
  IdentitiesOnly yes
CONF
  chmod 600 "$SSH_CONFIG"
  echo "Added github.com host config to $SSH_CONFIG" >&2
fi

echo "Generated new SSH key at $KEY_PATH" >&2
cat "${KEY_PATH}.pub"
