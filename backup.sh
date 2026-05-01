#!/bin/bash
# Daily VACUUM-INTO of EV Monitor SQLite databases.
# Backups land on the data volume (/mnt/ev-data/backups).
# Skips ladenetz_dynamic (>9 GB and not displayed on the dashboard).
#
# Disk-safety design (rev 2 after the 2026-05-01 lockup):
#  * Rotate OLD backups BEFORE making new ones, so freed space is reusable.
#  * Per-DB pre-check: skip a DB if free space < (db_size + SAFETY_GB).
#    The previous design only gated once at start, so the first big
#    DB (eco at 13 GB) ate the headroom and every subsequent VACUUM
#    failed mid-flight.
#  * Per-DB retention: dynamic DBs are large and grow; keep only the
#    last few. Static DBs are tiny; keep a longer window.
set -euo pipefail

BACKUP_DIR="/home/ev/backups"           # symlink -> /mnt/ev-data/backups
DATA_DIR="/home/ev/ev-monitor/data"     # symlink -> /mnt/ev-data/data
DATE=$(date -u +%Y-%m-%d)

# Per-class retention (count of backups to keep).
RETAIN_DYNAMIC=3      # eco/qwello/enbw/tesla — large, grow daily
RETAIN_STATIC=7       # *_static — tiny

# Refuse to start a per-DB VACUUM if free space falls below this margin
# above the source DB size. Keeps writers (collector/push) alive even
# during backup.
SAFETY_GB=5

SKIP_PATTERNS=("ladenetz_dynamic")

mkdir -p "$BACKUP_DIR"

free_gb() {
    df -k --output=avail "$BACKUP_DIR" | tail -1 | awk '{print int($1/1024/1024)}'
}

db_size_gb() {
    # Round UP so a 13.4 GB DB reads as 14 (we need at least its size free).
    local bytes
    bytes=$(stat -c '%s' "$1" 2>/dev/null || echo 0)
    awk -v b="$bytes" 'BEGIN { print int((b + 1073741823) / 1073741824) }'
}

# ── Step 1: Rotate OLD backups first ─────────────────────────────────
# Per-class retention by count, not mtime. Keeps the N most recent
# files matching each <prefix>_YYYY-MM-DD.sqlite pattern.
rotate_class() {
    local pattern="$1" keep="$2"
    # shellcheck disable=SC2012
    ls -1t "$BACKUP_DIR"/${pattern}_*.sqlite 2>/dev/null | tail -n +$((keep + 1)) | while read -r f; do
        echo "[$DATE] ROTATE remove $(basename "$f")"
        rm -f "$f"
    done
}

# Discover prefixes from current DBs and rotate.
for db in "$DATA_DIR"/*.sqlite; do
    [ -f "$db" ] || continue
    name=$(basename "$db" .sqlite)
    if [[ "$name" == *_dynamic ]]; then
        rotate_class "$name" "$RETAIN_DYNAMIC"
    else
        rotate_class "$name" "$RETAIN_STATIC"
    fi
done

# Clean up any partial -journal files left from a previous failed VACUUM.
find "$BACKUP_DIR" -maxdepth 1 -name '*.sqlite-journal' -delete 2>/dev/null || true

echo "[$DATE] Free after rotation: $(free_gb) GB."

# ── Step 2: VACUUM INTO each DB, gated on per-DB free space ─────────
ok=0; fail=0; skip=0; gated=0
for db in "$DATA_DIR"/*.sqlite; do
    [ -f "$db" ] || continue
    name=$(basename "$db" .sqlite)

    # Skip patterns (e.g. ladenetz_dynamic too large to back up).
    skip_this=0
    for pat in "${SKIP_PATTERNS[@]}"; do
        if [[ "$name" == *"$pat"* ]]; then skip_this=1; break; fi
    done
    if [ $skip_this -eq 1 ]; then
        echo "[$DATE] SKIP $name (matches skip pattern)."
        skip=$((skip+1))
        continue
    fi

    db_gb=$(db_size_gb "$db")
    avail=$(free_gb)
    needed=$(( db_gb + SAFETY_GB ))
    if [ "$avail" -lt "$needed" ]; then
        echo "[$DATE] GATE $name: only ${avail} GB free, need ${needed} GB (db=${db_gb} + safety=${SAFETY_GB}). Skipping." >&2
        gated=$((gated+1))
        continue
    fi

    dest="$BACKUP_DIR/${name}_${DATE}.sqlite"
    if sqlite3 "$db" "VACUUM INTO '$dest';" 2>/dev/null; then
        echo "[$DATE] OK   $name -> $(basename "$dest") ($(du -h "$dest" | cut -f1))"
        ok=$((ok+1))
    else
        echo "[$DATE] FAIL $name (removing partial file)." >&2
        rm -f "$dest"
        fail=$((fail+1))
    fi
done

echo "[$DATE] Done. ok=$ok fail=$fail skipped=$skip gated=$gated dynamic_retain=$RETAIN_DYNAMIC static_retain=$RETAIN_STATIC"
echo "[$DATE] Free on volume after run: $(free_gb) GB."

if [ "$(free_gb)" -lt "$SAFETY_GB" ]; then
    echo "[$DATE] WARNING: free space below ${SAFETY_GB} GB." >&2
fi

exit 0
