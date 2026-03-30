#!/usr/bin/env bash
# deploy.sh — Auto-pull from GitHub and restart services if code changed.
# Intended to run as a cron job every 5 minutes.
set -euo pipefail

REPO_DIR="/home/ev/ev-monitor"
VENV="$REPO_DIR/.venv"
LOG="/home/ev/deploy.log"

cd "$REPO_DIR"

LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploying $LOCAL → $REMOTE" >> "$LOG"

git reset --hard origin/main >> "$LOG" 2>&1
"$VENV/bin/pip" install --quiet -r requirements.txt >> "$LOG" 2>&1

sudo systemctl restart ev-eco ev-tesla ev-dashboard >> "$LOG" 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploy complete" >> "$LOG"
