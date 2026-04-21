"""Daily cleanup job for mailwatch.

Run via ``python -m mailwatch.cleanup`` from a systemd timer (or cron).
Purges:

* ``scan_events`` older than 60 days — telemetry, not truth data.
* ``serial_counters`` older than 48 hours — only the current day's row
  is load-bearing; stale days can be reclaimed.
* ``address_cache`` older than 1 year — USPS delivery-point data does
  drift, so let entries re-standardize annually.

After the DELETEs the script runs ``PRAGMA wal_checkpoint(TRUNCATE)`` to
bound WAL growth. It deliberately does *not* run ``VACUUM`` — that
rewrites the DB file and invalidates the Litestream generation, forcing
a full re-snapshot. See :func:`mailwatch.db.purge_old` for the rationale.
"""

from __future__ import annotations

import logging

from mailwatch import db
from mailwatch.config import get_settings


def main() -> dict[str, int]:
    """Run one cleanup pass and return the per-table delete counts.

    ``init_db`` runs first so the script is safe to execute on a brand
    new host before the web app has ever started — the timer can fire in
    any order relative to the first request.
    """
    settings = get_settings()
    conn = db.connect(settings.DB_PATH)
    try:
        db.init_db(conn)
        deleted = db.purge_old(conn)
        logging.info("cleanup: deleted=%s", deleted)
        return deleted
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
