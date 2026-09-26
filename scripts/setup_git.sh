#!/bin/bash
# Configure git for the autopilot user and test SSH to GitHub.
set -e

echo "=== Configuring git ==="
git config --global user.name "TrueSight DAO Autopilot"
git config --global user.email "admin@truesight.me"
git config --global core.editor "nano"
git config --global init.defaultBranch main

echo "=== Testing SSH connection to GitHub ==="
ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true

echo "=== Cloning key repos ==="
cd /opt
for repo in agentic_ai_context dao_client truesight_autopilot; do
  if [ ! -d "$repo" ]; then
    echo "Cloning $repo..."
    git clone "git@github.com:TrueSightDAO/$repo.git"
  else
    echo "$repo already exists, pulling latest..."
    cd "$repo" && git pull && cd /opt
  fi
done

echo "=== Done ==="
echo "Repos at /opt/:"
ls -d /opt/*/
