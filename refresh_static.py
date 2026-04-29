"""Daily static-metadata refresh for all registered providers.

The push receiver subscribes to both static and dynamic Mobilithek feeds,
but operators occasionally republish static metadata at intervals longer
than a day or temporarily stop pushing it. To keep the on-disk static
DBs from going stale, this script unconditionally pulls a fresh static
snapshot for every provider in ``providers.PROVIDERS`` and upserts it
into the ``charging_points`` table.

Unlike ``collector.store_static_snapshot`` (which uses plain INSERT and
trips the PRIMARY KEY on point_id when called twice), this script does
an ``INSERT OR REPLACE`` so re-running daily is idempotent: changing
fields (e.g. station_max_power_w) update in place, new point_ids appear,
and old ones stay until the operator removes them upstream.

Intended to be run from a systemd timer once per day. Exit code is 0
if at least one provider succeeded, 1 if every provider failed (so the
timer surfaces a hard failure but does not flap on transient single-
provider hiccups).

Usage:
    python refresh_static.py                  # all providers
    python refresh_static.py eco_movement tesla  # subset
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

import collector as col
from providers import PROVIDERS, get_provider

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOGGER = logging.getLogger("refresh_static")


def upsert_static(
    conn: sqlite3.Connection,
    points_df: pd.DataFrame,
    publication_time: pd.Timestamp,
) -> int:
    """INSERT OR REPLACE charging_points + bump static_meta timestamps."""
    deduped = points_df.drop_duplicates(subset=["point_id"], keep="last").copy()
    deduped["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()

    # Restrict to columns that actually exist on the table to avoid
    # surprising the user with a schema mismatch if the provider added
    # new fields the static DDL doesn't know about yet.
    table_cols = {
        r[1] for r in conn.execute(
            "PRAGMA table_info(charging_points)"
        ).fetchall()
    }
    cols = [c for c in deduped.columns if c in table_cols]
    if not cols:
        raise RuntimeError("No overlap between dataframe and charging_points table")

    placeholders = ",".join(["?"] * len(cols))
    col_list = ",".join(cols)
    sql = f"INSERT OR REPLACE INTO charging_points ({col_list}) VALUES ({placeholders})"

    # Replace pandas NA / NaN with None so sqlite3 can bind them.
    sanitised = deduped[cols].astype(object).where(pd.notna(deduped[cols]), None)
    rows = list(sanitised.itertuples(index=False, name=None))
    conn.executemany(sql, rows)
    conn.execute(
        "INSERT OR REPLACE INTO static_meta (key, value) VALUES (?, ?)",
        ("last_publication_time", publication_time.isoformat()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO static_meta (key, value) VALUES (?, ?)",
        ("last_fetched_utc", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return len(rows)


def refresh_one(name: str) -> bool:
    """Pull static snapshot for one provider; return True on success."""
    try:
        provider = get_provider(name)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return False

    LOGGER.info("Refreshing static data for %s ...", name)
    started = datetime.now(timezone.utc)
    try:
        static_conn = col.ensure_static_db(provider)
    except Exception:
        LOGGER.exception("Failed to open static DB for %s", name)
        return False

    try:
        points_df, pub_time = provider.load_static_snapshot()
        n = upsert_static(static_conn, points_df, pub_time)
    except Exception:
        LOGGER.exception("Failed to refresh static data for %s", name)
        return False
    finally:
        static_conn.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    LOGGER.info("OK %s: %d points (%.1fs, pub_time=%s)", name, n, elapsed, pub_time)
    return True


def main(argv: list[str]) -> int:
    targets = argv[1:] if len(argv) > 1 else sorted(PROVIDERS)
    LOGGER.info("Targets: %s", ", ".join(targets))

    successes = 0
    for name in targets:
        if refresh_one(name):
            successes += 1

    LOGGER.info("Done: %d/%d providers refreshed", successes, len(targets))
    return 0 if successes > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
