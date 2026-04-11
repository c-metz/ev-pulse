"""Unified collector for Mobilithek charging-data providers.

Collects static infrastructure metadata and dynamic status updates into
per-provider SQLite databases, using change-detection to record only
actual status transitions.

Usage:
    python collector.py tesla                   # run Tesla continuously
    python collector.py eco_movement            # run eco-movement continuously
    python collector.py tesla --once            # one cycle and exit
    python collector.py tesla --compact         # deduplicate dynamic history
    python collector.py tesla --verbose         # debug output
    python collector.py --list                  # show available providers
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
import time

import pandas as pd

from providers import get_provider, list_providers
from providers.base import Provider

LOGGER = logging.getLogger("collector")
KEY_COLUMNS = ["point_id"]

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_STATIC_REFRESH_HOURS = 12


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ═══════════════════════════════════════════════════════════════════════
#  STATIC DATABASE
# ═══════════════════════════════════════════════════════════════════════
def ensure_static_db(provider: Provider) -> sqlite3.Connection:
    db_path = provider.static_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS static_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.executescript(provider.static_table_ddl())

    # Migration: add columns that may not exist in older static databases
    static_cols = {row[1] for row in conn.execute("PRAGMA table_info(charging_points)").fetchall()}
    new_static_cols = {
        "evse_id": "TEXT",
        "auth_methods": "TEXT",
        "service_type": "TEXT",
        "usage_type": "TEXT",
        "num_connectors": "INTEGER",
    }
    for new_col, col_type in new_static_cols.items():
        if new_col not in static_cols:
            LOGGER.info("Migrating charging_points: adding column '%s'", new_col)
            conn.execute(f"ALTER TABLE charging_points ADD COLUMN {new_col} {col_type}")

    conn.commit()
    return conn


def store_static_snapshot(
    conn: sqlite3.Connection,
    points_df: pd.DataFrame,
    publication_time: pd.Timestamp,
) -> None:
    deduped = points_df.drop_duplicates(subset=["point_id"], keep="last")
    conn.execute("DELETE FROM charging_points")
    deduped.to_sql("charging_points", conn, if_exists="append", index=False)
    conn.execute(
        "INSERT OR REPLACE INTO static_meta (key, value) VALUES (?, ?)",
        ("last_publication_time", publication_time.isoformat()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO static_meta (key, value) VALUES (?, ?)",
        ("last_fetched_utc", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    LOGGER.info(
        "Static DB updated: %d points (%d raw, %d dupes removed), pub %s",
        len(deduped), len(points_df), len(points_df) - len(deduped),
        publication_time.isoformat(),
    )


# ═══════════════════════════════════════════════════════════════════════
#  DYNAMIC DATABASE
# ═══════════════════════════════════════════════════════════════════════
def ensure_dynamic_db(provider: Provider) -> sqlite3.Connection:
    db_path = provider.dynamic_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    in_use_col = provider.in_use_count_column
    tracked_cols_ddl = ",\n            ".join(
        f"{col} TEXT" for col in provider.tracked_columns
    )

    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS dynamic_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshot_runs (
            snapshot_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at_utc     TEXT NOT NULL,
            publication_time_utc TEXT NOT NULL,
            delivery_type        TEXT NOT NULL,
            point_count          INTEGER NOT NULL,
            {in_use_col}         INTEGER NOT NULL,
            available_count      INTEGER NOT NULL,
            unknown_count        INTEGER NOT NULL,
            estimated_power_mw   REAL
        );

        CREATE TABLE IF NOT EXISTS point_status_history (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id          INTEGER NOT NULL REFERENCES snapshot_runs(snapshot_id),
            collected_at_utc     TEXT NOT NULL,
            publication_time_utc TEXT,
            point_id             TEXT NOT NULL,
            {tracked_cols_ddl}
        );

        CREATE INDEX IF NOT EXISTS idx_psh_point_time
            ON point_status_history(point_id, collected_at_utc);
        CREATE INDEX IF NOT EXISTS idx_psh_time
            ON point_status_history(collected_at_utc);
        CREATE INDEX IF NOT EXISTS idx_psh_snapshot
            ON point_status_history(snapshot_id);
    """)

    # Migrations: add columns that may not exist in older databases
    sr_cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshot_runs)").fetchall()}
    if "estimated_power_mw" not in sr_cols:
        conn.execute("ALTER TABLE snapshot_runs ADD COLUMN estimated_power_mw REAL")

    psh_cols = {row[1] for row in conn.execute("PRAGMA table_info(point_status_history)").fetchall()}
    for col in provider.tracked_columns:
        if col not in psh_cols:
            LOGGER.info("Migrating point_status_history: adding column '%s'", col)
            conn.execute(f"ALTER TABLE point_status_history ADD COLUMN {col} TEXT")

    conn.commit()
    return conn


def load_last_known_state(
    conn: sqlite3.Connection, provider: Provider,
) -> pd.DataFrame | None:
    has_data = conn.execute(
        "SELECT 1 FROM point_status_history LIMIT 1"
    ).fetchone()
    if not has_data:
        return None
    cols = ", ".join(f"h.{col}" for col in provider.tracked_columns)
    return pd.read_sql_query(
        f"""
        SELECT h.point_id, {cols}
        FROM point_status_history h
        INNER JOIN (
            SELECT point_id, MAX(id) AS max_id
            FROM point_status_history
            GROUP BY point_id
        ) latest ON h.id = latest.max_id
        """,
        conn,
    )


def get_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM dynamic_meta WHERE key = 'last_modified_cursor'"
    ).fetchone()
    return row[0] if row else None


def set_cursor(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO dynamic_meta (key, value) VALUES (?, ?)",
        ("last_modified_cursor", value),
    )
    conn.commit()


def find_changed_rows(
    current_df: pd.DataFrame,
    previous_state: pd.DataFrame | None,
    tracked_columns: list[str],
) -> pd.DataFrame:
    if previous_state is None or previous_state.empty:
        return current_df

    merged = current_df.merge(
        previous_state[KEY_COLUMNS + tracked_columns],
        on=KEY_COLUMNS,
        how="left",
        suffixes=("", "_prev"),
    )
    changed = pd.Series(False, index=merged.index)
    for col in tracked_columns:
        new_vals = merged[col].astype(object).fillna("__NONE__").astype(str)
        old_vals = merged[f"{col}_prev"].astype(object).fillna("__NONE__").astype(str)
        changed |= new_vals != old_vals
    return current_df.loc[changed]


def store_dynamic_snapshot(
    conn: sqlite3.Connection,
    provider: Provider,
    status_df: pd.DataFrame,
    pub_time: str | None,
    delivery_type: str,
    previous_state: pd.DataFrame | None,
    static_conn: sqlite3.Connection | None = None,
) -> tuple[int | None, pd.DataFrame]:
    collected_at = datetime.now(timezone.utc).isoformat()
    tracked = provider.tracked_columns

    # ── Step 1: Compute the full updated state ───────────────────────
    changed_df = find_changed_rows(status_df, previous_state, tracked)

    # Skip no-op DELTA deliveries: nothing changed, nothing to record.
    # SNAPSHOTs are never skipped — they confirm/reset ground truth.
    if (changed_df.empty
            and delivery_type != "SNAPSHOT"
            and previous_state is not None
            and not previous_state.empty):
        LOGGER.debug(
            "No-op %s for %s — 0 changes, skipping snapshot_run.",
            delivery_type, provider.name,
        )
        return None, previous_state

    if previous_state is not None and not previous_state.empty:
        updated = previous_state.copy()
        for _, row in changed_df.iterrows():
            mask = updated["point_id"] == row["point_id"]
            if mask.any():
                for col in tracked:
                    updated.loc[mask, col] = row[col]
            else:
                updated = pd.concat(
                    [updated, row[KEY_COLUMNS + tracked].to_frame().T],
                    ignore_index=True,
                )
    else:
        updated = status_df[KEY_COLUMNS + tracked].copy()

    # ── Step 2: Counts from the FULL state (not just the delivery) ───
    in_use = int(updated["status"].eq(provider.in_use_status).sum())
    available = int(updated["status"].eq("available").sum())
    unknown = int(updated["status"].eq("unknown").sum())

    # ── Step 2b: Estimated power (MW) from static rated power ────────
    estimated_power_mw = None
    if static_conn is not None:
        try:
            in_use_ids = updated.loc[
                updated["status"] == provider.in_use_status, "point_id"
            ].tolist()
            if in_use_ids:
                placeholders = ",".join("?" * len(in_use_ids))
                pcol = provider.power_column
                row = static_conn.execute(
                    f"SELECT SUM({pcol}) FROM charging_points "
                    f"WHERE point_id IN ({placeholders})",
                    in_use_ids,
                ).fetchone()
                estimated_power_mw = (row[0] or 0.0) * provider.power_to_mw
            else:
                estimated_power_mw = 0.0
        except Exception:
            LOGGER.debug("Could not compute estimated power", exc_info=True)

    # ── Step 3: Insert snapshot_run with full-state counts ───────────
    in_use_col = provider.in_use_count_column
    cursor = conn.execute(
        f"""
        INSERT INTO snapshot_runs
            (collected_at_utc, publication_time_utc, delivery_type,
             point_count, {in_use_col}, available_count, unknown_count,
             estimated_power_mw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (collected_at, pub_time or "", delivery_type,
         len(updated), in_use, available, unknown, estimated_power_mw),
    )
    snapshot_id = cursor.lastrowid

    # ── Step 4: Insert only changed rows into history ────────────────
    if not changed_df.empty:
        insert_df = changed_df[KEY_COLUMNS + tracked].copy()
        insert_df.insert(0, "snapshot_id", snapshot_id)
        insert_df.insert(1, "collected_at_utc", collected_at)
        insert_df.insert(2, "publication_time_utc", pub_time or "")
        insert_df = insert_df.where(pd.notna(insert_df), None)
        insert_df.to_sql(
            "point_status_history", conn, if_exists="append", index=False,
        )

    conn.commit()

    LOGGER.info(
        "Snapshot #%d (%s): %d total, %d changed, %d in-use, %d available",
        snapshot_id, delivery_type, len(updated), len(changed_df),
        in_use, available,
    )

    return snapshot_id, updated


def compact_history(conn: sqlite3.Connection, provider: Provider) -> int:
    parts = [f"COALESCE({col},'')" for col in provider.tracked_columns]
    fingerprint = " || '|' || ".join(parts)
    cursor = conn.execute(f"""
        DELETE FROM point_status_history
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                    {fingerprint} AS fp,
                    LAG({fingerprint})
                        OVER (PARTITION BY point_id ORDER BY id) AS prev_fp
                FROM point_status_history
            )
            WHERE prev_fp IS NOT NULL AND fp = prev_fp
        )
    """)
    removed = cursor.rowcount
    conn.commit()
    if removed > 0:
        conn.execute("VACUUM")
    return removed


# ═══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════
def refresh_static_if_needed(
    provider: Provider,
    static_conn: sqlite3.Connection,
    last_refresh: datetime | None,
    refresh_interval: timedelta,
) -> datetime:
    now = datetime.now(timezone.utc)
    if last_refresh and now - last_refresh < refresh_interval:
        return last_refresh
    try:
        points_df, pub_time = provider.load_static_snapshot()
        store_static_snapshot(static_conn, points_df, pub_time)
        return now
    except Exception:
        LOGGER.exception("Failed to refresh static data for %s.", provider.name)
        return last_refresh or now


def collect_dynamic_once(
    provider: Provider,
    dynamic_conn: sqlite3.Connection,
    previous_state: pd.DataFrame | None,
    static_conn: sqlite3.Connection | None = None,
) -> pd.DataFrame | None:
    cursor = get_cursor(dynamic_conn)

    if cursor is None:
        cursor = "Thu, 01 Jan 1970 00:00:00 GMT"

    deliveries = provider.drain_dynamic_deliveries(
        if_modified_since=cursor, max_deliveries=500,
    )

    if not deliveries:
        LOGGER.debug("No new deliveries for %s.", provider.name)
        return previous_state

    current_state = previous_state

    for delivery in deliveries:
        status_df, pub_time = provider.parse_dynamic_points(delivery["data"])
        if status_df.empty:
            continue

        if delivery["type"] == "SNAPSHOT":
            # Full snapshot: contains ALL points → replaces full state
            _, current_state = store_dynamic_snapshot(
                dynamic_conn, provider, status_df, pub_time,
                delivery["type"], previous_state=None,
                static_conn=static_conn,
            )
        else:
            # Delta: contains only changed points → merge into state
            _, current_state = store_dynamic_snapshot(
                dynamic_conn, provider, status_df, pub_time,
                delivery["type"], previous_state=current_state,
                static_conn=static_conn,
            )

    last_lm = deliveries[-1].get("last_modified")
    if last_lm:
        set_cursor(dynamic_conn, last_lm)

    return current_state


def run_collector(
    provider: Provider,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    static_refresh_hours: int = DEFAULT_STATIC_REFRESH_HOURS,
    run_once: bool = False,
) -> None:
    static_conn = ensure_static_db(provider)
    dynamic_conn = ensure_dynamic_db(provider)

    refresh_interval = timedelta(hours=static_refresh_hours)
    last_static_refresh: datetime | None = None
    previous_state = load_last_known_state(dynamic_conn, provider)

    if previous_state is not None:
        LOGGER.info(
            "%s: resumed with %d known point states.",
            provider.name, len(previous_state),
        )

    try:
        while True:
            t0 = time.monotonic()

            last_static_refresh = refresh_static_if_needed(
                provider, static_conn, last_static_refresh, refresh_interval,
            )

            try:
                previous_state = collect_dynamic_once(
                    provider, dynamic_conn, previous_state,
                    static_conn=static_conn,
                )
            except Exception:
                LOGGER.exception(
                    "Dynamic collection failed for %s, retrying next cycle.",
                    provider.name,
                )

            if run_once:
                return

            elapsed = time.monotonic() - t0
            sleep_time = max(0.0, interval_seconds - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)
    finally:
        static_conn.close()
        dynamic_conn.close()


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect charging data from Mobilithek into SQLite.",
    )
    parser.add_argument(
        "provider", nargs="?",
        help="Provider name (see --list).",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available providers and exit.",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        help=f"Dynamic polling interval in seconds (default: {DEFAULT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--static-refresh-hours", type=int, default=DEFAULT_STATIC_REFRESH_HOURS,
        help=f"Static data refresh interval in hours (default: {DEFAULT_STATIC_REFRESH_HOURS}).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one collection cycle and exit.",
    )
    parser.add_argument(
        "--compact", action="store_true",
        help="Remove redundant rows from dynamic history and exit.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    if args.list:
        print("Available providers:")
        for name in list_providers():
            p = get_provider(name)
            print(f"  {name:20s}  {p.name}")
        return

    if not args.provider:
        print("Error: provider name required. Use --list to see available providers.")
        raise SystemExit(1)

    provider = get_provider(args.provider)

    if args.compact:
        conn = ensure_dynamic_db(provider)
        LOGGER.info("Compacting %s dynamic history …", provider.name)
        removed = compact_history(conn, provider)
        LOGGER.info("Removed %d redundant rows.", removed)
        conn.close()
        return

    run_collector(
        provider,
        interval_seconds=args.interval,
        static_refresh_hours=args.static_refresh_hours,
        run_once=args.once,
    )


if __name__ == "__main__":
    main()
