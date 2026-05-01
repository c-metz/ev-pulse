#!/usr/bin/env bash
# deploy.sh -- Auto-pull from GitHub and restart only the services whose
# code actually changed. Intended to run from cron every 5 minutes on
# the collection server.
#
# Why selective restarts: ev-push handles real-time DATEX II push events.
# Restarting it on every commit (e.g. docs-only changes) drops in-flight
# HTTP requests for no reason. We map changed paths -> services and only
# restart what's necessary.
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

# Compute the change list BEFORE reset so we still have both refs reachable.
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")

{
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploying $LOCAL -> $REMOTE"
    echo "Changed:"
    echo "$CHANGED" | sed 's/^/  /'
} >> "$LOG"

git reset --hard origin/main >> "$LOG" 2>&1
"$VENV/bin/pip" install --quiet -r requirements.txt >> "$LOG" 2>&1

changed() { grep -qxF "$1" <<< "$CHANGED"; }
changed_dir() { grep -q "^$1" <<< "$CHANGED"; }

# Services to restart (deduped via associative array keys).
declare -A RESTART=()

# Shared Python code: anything that changes here can affect every service.
if changed providers/__init__.py \
   || changed providers/base.py \
   || changed collector.py \
   || changed requirements.txt; then
    RESTART[ev-eco]=1
    RESTART[ev-tesla]=1
    RESTART[ev-ladenetz]=1
    RESTART[ev-qwello]=1
    RESTART[ev-smatrics]=1
    RESTART[ev-push]=1
    RESTART[ev-dashboard]=1
fi

# Per-service code paths.
if changed providers/eco_movement.py; then RESTART[ev-eco]=1; fi
if changed providers/tesla.py;        then RESTART[ev-tesla]=1; fi
if changed providers/ladenetz.py;     then RESTART[ev-ladenetz]=1; fi
if changed providers/qwello.py;       then RESTART[ev-qwello]=1; fi
if changed providers/smatrics.py;     then RESTART[ev-smatrics]=1; fi
if changed push_receiver.py;          then RESTART[ev-push]=1; fi
if changed dashboard.py;              then RESTART[ev-dashboard]=1; fi

# Sync backup.sh to /home/ev/backup.sh (where ev's crontab calls it).
# Doing it via install -m 755 keeps the script executable and lets us
# version-control it alongside the code.
if changed backup.sh; then
    install -m 755 "$REPO_DIR/backup.sh" "$HOME/backup.sh" 2>> "$LOG"
    echo "Synced backup.sh to $HOME/backup.sh" >> "$LOG"
fi

# Sync systemd unit files when anything in systemd/ changed.
if changed_dir systemd/; then
    if compgen -G "$REPO_DIR/systemd/*" > /dev/null; then
        # set +e so a single bad unit doesn't abort the whole deploy.
        set +e
        sudo install -m 644 -t /etc/systemd/system/ "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer 2>> "$LOG"
        sudo systemctl daemon-reload >> "$LOG" 2>&1
        # Restart timers so any schedule change takes effect now.
        for unit in "$REPO_DIR"/systemd/*.timer; do
            [ -f "$unit" ] || continue
            sudo systemctl restart "$(basename "$unit")" >> "$LOG" 2>&1
        done
        set -e
        echo "Synced systemd units" >> "$LOG"
    fi
fi

if [ "${#RESTART[@]}" -gt 0 ]; then
    SERVICES="${!RESTART[*]}"
    echo "Restarting: $SERVICES" >> "$LOG"
    sudo systemctl restart $SERVICES >> "$LOG" 2>&1
else
    echo "No service restart needed" >> "$LOG"
fi

echo "$(date -u '+%Y-%m-%d %H:%M:%S') Deploy complete" >> "$LOG"
