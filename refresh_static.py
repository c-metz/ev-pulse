"""Daily static-metadata refresh for all registered providers.

The push receiver subscribes to both static and dynamic Mobilithek feeds,
but operators occasionally republish static metadata at intervals longer
than a day or temporarily stop pushing it. To keep the on-disk static
DBs from going stale, this script unconditionally pulls a fresh static
snapshot for every provider in ``providers.PROVIDERS`` via the canonical
``collector.store_static_snapshot`` helper (which uses INSERT OR REPLACE
on the point_id PK so re-running is idempotent).

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
import sys
from datetime import datetime, timezone

import collector as col
from providers import PROVIDERS, get_provider

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOGGER = logging.getLogger("refresh_static")


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
        col.store_static_snapshot(static_conn, points_df, pub_time)
    except Exception:
        LOGGER.exception("Failed to refresh static data for %s", name)
        return False
    finally:
        static_conn.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    LOGGER.info("OK %s (%.1fs, pub_time=%s)", name, elapsed, pub_time)
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
