#!/usr/bin/env bash
# deploy.sh -- Auto-pull from GitHub and restart services if code changed.
# Intended to run as a cron job every 5 minutes on the collection server.
#
# Adjust REPO_DIR, VENV, and service names to match your deployment.
set -euo pipefail

REPO_DIR="${EV_PULSE_DIR:-$HOME/ev-pulse}"
VENV="$REPO_DIR/.venv"
LOG="$REPO_DIR/deploy.log"

cd "$REPO_DIR"

LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploying $LOCAL -> $REMOTE" >> "$LOG"

git reset --hard origin/main >> "$LOG" 2>&1
"$VENV/bin/pip" install --quiet -r requirements.txt >> "$LOG" 2>&1

# Restart collector and dashboard services (adjust names to your setup)
sudo systemctl restart ev-eco ev-tesla ev-dashboard >> "$LOG" 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploy complete" >> "$LOG"
