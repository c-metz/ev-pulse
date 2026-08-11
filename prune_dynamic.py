"""Daily retention pruning for dynamic provider DBs.

Deletes rows in ``point_status_history`` and ``snapshot_runs`` older than
the configured retention window. Operates in chunked DELETEs with WAL
checkpoints between batches so the WAL doesn't balloon and concurrent
writers (collector / push receiver) aren't starved for the writer lock
during a multi-million-row delete.

**No VACUUM is performed.** The DB file size won't shrink, but freed
pages get reused by future inserts, so net growth halts. This is the
disk-cost-vs-runtime trade we prefer: a VACUUM on eco_dynamic would need
~2x the file size in free disk and would block writers for ~10 minutes,
whereas chunked DELETE keeps writers happy and the disk-budget problem
is solved either way.

Intended to run from a systemd timer once per day, after backup.sh has
captured the day's snapshot.

Usage::

    python prune_dynamic.py                       # all dynamic providers, 7 days
    python prune_dynamic.py --days 14             # custom retention
    python prune_dynamic.py eco_movement          # one provider
    python prune_dynamic.py --dry-run             # count only, don't delete
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from providers import get_provider

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOGGER = logging.getLogger("prune_dynamic")

DEFAULT_DAYS = 7
BATCH_SIZE = 50_000
# Tiny breather between batches so the collector / push receiver gets
# turns at the writer lock. 0.0 if you want maximum throughput.
SLEEP_BETWEEN_BATCHES_SECONDS = 0.05
# How often to run a passive WAL checkpoint. The WAL grows on every
# committed batch; checkpointing periodically keeps it from filling
# the disk during a multi-hour initial cleanup.
CHECKPOINT_EVERY_N_BATCHES = 20

# Providers with a dynamic DB. Tesla / EnBW are small today, but we
# include them so the retention policy is uniform — easier to reason
# about than per-provider exceptions.
DYNAMIC_PROVIDERS = [
    "eco_movement",
    "tesla",
    "ladenetz",
    "qwello",
    "smatrics",
    "enbw",
]


def prune_one(name: str, days: int, dry_run: bool, before: str | None = None) -> bool:
    """Prune one provider's dynamic DB; return True on success.

    ``before`` overrides ``days`` when provided. Use it for surgical
    one-shot cleanups (e.g., removing a specific bad-data window) where
    a relative window from "now" would be either too coarse or would
    keep shifting on subsequent runs.
    """
    try:
        provider = get_provider(name)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return False

    if before is not None:
        cutoff_iso = before
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat(timespec="seconds")

    db_path = provider.dynamic_db_path
    if not db_path.exists():
        LOGGER.warning("%s: dynamic DB not found at %s, skipping.", name, db_path)
        return False

    LOGGER.info("=== %s: prune rows older than %s ===", name, cutoff_iso)

    # busy_timeout matches the ~60s collector poll interval so the
    # collector's writes don't immediately fail us during a long batch.
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")

        psh_count = conn.execute(
            "SELECT COUNT(*) FROM point_status_history WHERE collected_at_utc < ?",
            (cutoff_iso,),
        ).fetchone()[0]
        sr_count = conn.execute(
            "SELECT COUNT(*) FROM snapshot_runs WHERE collected_at_utc < ?",
            (cutoff_iso,),
        ).fetchone()[0]

        LOGGER.info(
            "%s: would prune %d point_status_history rows, %d snapshot_runs rows",
            name, psh_count, sr_count,
        )
        if dry_run or (psh_count == 0 and sr_count == 0):
            return True

        # ── Step 1: history (huge, must be batched) ─────────────────
        # We delete by rowid via a sub-SELECT that uses idx_psh_time
        # for the time predicate. LIMIT bounds each transaction so the
        # WAL stays small between checkpoints.
        deleted = 0
        batch = 0
        t0 = time.monotonic()
        while True:
            cur = conn.execute(
                "DELETE FROM point_status_history "
                "WHERE rowid IN ("
                "  SELECT rowid FROM point_status_history "
                "  WHERE collected_at_utc < ? "
                "  LIMIT ?"
                ")",
                (cutoff_iso, BATCH_SIZE),
            )
            n = cur.rowcount
            conn.commit()
            if n == 0:
                break
            deleted += n
            batch += 1
            if batch % CHECKPOINT_EVERY_N_BATCHES == 0:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                rate = deleted / max(time.monotonic() - t0, 0.001)
                LOGGER.info(
                    "%s: %d/%d history rows pruned (%.0f rows/s)",
                    name, deleted, psh_count, rate,
                )
            if SLEEP_BETWEEN_BATCHES_SECONDS:
                time.sleep(SLEEP_BETWEEN_BATCHES_SECONDS)

        # ── Step 2: snapshot_runs (small enough for one statement) ──
        cur = conn.execute(
            "DELETE FROM snapshot_runs WHERE collected_at_utc < ?",
            (cutoff_iso,),
        )
        sr_deleted = cur.rowcount
        conn.commit()
        # Final checkpoint so the WAL doesn't carry the whole prune
        # forward into normal collector operation.
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        elapsed = time.monotonic() - t0
        LOGGER.info(
            "%s: pruned %d history rows + %d snapshot_runs in %.1fs",
            name, deleted, sr_deleted, elapsed,
        )
        return True
    except Exception:
        LOGGER.exception("Prune failed for %s", name)
        return False
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prune old rows from dynamic provider DBs.",
    )
    parser.add_argument(
        "providers", nargs="*",
        help="Provider names (default: all dynamic providers).",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Retention window in days (default {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows without deleting.",
    )
    parser.add_argument(
        "--before", type=str, default=None,
        help=(
            "Explicit ISO timestamp cutoff (e.g. 2026-04-30T00:00:00+00:00). "
            "Overrides --days. Use for one-shot surgical cleanups."
        ),
    )
    args = parser.parse_args(argv[1:])

    targets = args.providers or DYNAMIC_PROVIDERS
    LOGGER.info(
        "Targets: %s; %s; dry_run=%s",
        ", ".join(targets),
        f"before={args.before}" if args.before else f"days={args.days}",
        args.dry_run,
    )

    successes = sum(
        1 for n in targets
        if prune_one(n, args.days, args.dry_run, before=args.before)
    )
    LOGGER.info("Done: %d/%d providers pruned", successes, len(targets))
    return 0 if successes > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
