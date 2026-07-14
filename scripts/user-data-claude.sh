#!/bin/bash
# Cloud-init user-data for the NELANCO CLAUDE CODE BOX (first-boot).
#
# This box runs PLAIN INTERACTIVE Claude Code (driven from the Claude mobile app
# via `claude --remote-control`), NOT the autopilot service. It mirrors Sophia's
# ENVIRONMENT (toolchain + creds + fleet SSH) but installs none of her services.
# Full design: agentic_ai_context/plans/NELANCO_CLAUDE_CODE_BOX_PLAN.md
#
# What this does: base toolchain for env parity + tmux + Claude Code, an SSH
# authorized_key so the operator Mac can `ssh nelanco-claude`, and passwordless
# sudo. Credentials, repo clones, and the fleet SSH key are laid down AFTER boot
# by the Gate-C laydown step (see the plan), not here.

set -e

apt-get update -y
apt-get upgrade -y

# Base toolchain — kept in parity with Sophia's box so any workspace script runs.
# (python/venv, node+clasp for GAS, ffmpeg + tesseract + poppler for media/OCR.)
# tmux is the key addition: the operator SSHes in, starts `claude` in tmux, and
# enables remote-control; tmux keeps the session alive across SSH disconnects.
# nginx is intentionally OMITTED (no web service on this box).
apt-get install -y python3.11 python3.11-venv python3-pip git tmux jq \
    ffmpeg tesseract-ocr poppler-utils

# Node 20 + clasp (GAS deploys; clasp OAuth token synced later by laydown).
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @google/clasp@3.3.0

# Claude Code CLI. Auth is done interactively at Gate C (`claude` login as the
# operator's Claude account — Pro/Max/Team required for the mobile/remote-control
# experience). Best-effort so a transient npm hiccup doesn't abort first boot.
npm install -g @anthropic-ai/claude-code || \
    echo "WARN: claude-code install failed at boot — install manually at Gate C"

# Workspace lives under /opt (parity with Sophia's /opt/truesight_autopilot).
mkdir -p /opt/claude_workspace
chown ubuntu:ubuntu /opt/claude_workspace

# Operator SSH access: authorize the same agentic_ai_github key the operator Mac
# uses, so `ssh nelanco-claude` works from Gary's laptop.
mkdir -p /home/ubuntu/.ssh
chmod 700 /home/ubuntu/.ssh
cat >> /home/ubuntu/.ssh/authorized_keys << 'PUBKEY'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILVeXXbMiSGb3c0TQCmhzb7deVdm+De29bxCLHTsVc/m agentic-ai-github-TrueSightDAO
PUBKEY
chown -R ubuntu:ubuntu /home/ubuntu/.ssh
chmod 600 /home/ubuntu/.ssh/authorized_keys

# Passwordless sudo for ubuntu (convenience; parity with Sophia's box).
echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/99-ubuntu
chmod 440 /etc/sudoers.d/99-ubuntu

echo "Setup complete — next: run the Gate-C laydown (creds + repos + fleet SSH key)."
