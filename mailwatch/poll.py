"""Periodic IV-MTR pull-poll for all in-flight IMbs.

Invoked as ``python -m mailwatch.poll``. A systemd timer (or cron) calls this
every ~30 min. Each invocation queries IV-MTR for barcodes with recent
activity, stores any new scan events, then exits. One-shot, not long-running.

Idempotent via ``INSERT OR IGNORE`` on a synthetic ``event_id`` (sha256 of
``imb + scanDatetime + scanEventCode``) so re-runs and overlap with the push
feed produce no duplicates.

The push path (``routes.post_usps_feed``) stores events keyed on the USPS-
provided ``eventId``; the pull path doesn't get that field, so we derive a
deterministic stand-in. Event IDs never collide between sources (different
hash universes) so an event seen via both paths would land twice in SQLite
as two rows — acceptable: downstream UI merges by ``scan_datetime``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import sys
from typing import Final

import httpx

from mailwatch import db
from mailwatch.config import Settings, get_settings
from mailwatch.models import TrackingScan
from mailwatch.usps_api import IVMTRClient

log = logging.getLogger("mailwatch.poll")

DEFAULT_LOOKBACK_DAYS: Final = 14
HTTP_TIMEOUT_SECONDS: Final = 30.0


def _synth_event_id(imb: str, scan: TrackingScan) -> str:
    """Stable synthetic ``event_id`` for pull-sourced scans.

    IV-MTR pull responses don't carry an ``eventId`` the way push-feed events
    do. We hash ``imb + scanDatetime + scanEventCode`` so that rerunning the
    poll against the same (unchanging) tracking history is a no-op under the
    ``INSERT OR IGNORE`` in :func:`mailwatch.db.store_scan_event`.
    """
    digest_input = f"{imb}|{scan.scanDatetime.isoformat()}|{scan.scanEventCode}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


async def _poll_once(
    settings: Settings,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """Run a single poll pass. Returns ``{polled, new_events, errors}``.

    ``http_client`` is injectable so tests can supply an
    :class:`httpx.MockTransport`-backed client; in production the default
    branch constructs a fresh client per invocation (short-lived script).
    """
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    try:
        in_flight = db.get_in_flight_imbs(conn, lookback_days=lookback_days)
        if not in_flight:
            return {"polled": 0, "new_events": 0, "errors": 0}

        own_client = http_client is None
        if http_client is None:
            http_client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                trust_env=False,
            )
        try:
            return await _run_poll(settings, conn, http_client, in_flight)
        finally:
            if own_client:
                await http_client.aclose()
    finally:
        conn.close()


async def _run_poll(
    settings: Settings,
    conn: sqlite3.Connection,
    http_client: httpx.AsyncClient,
    in_flight: list[str],
) -> dict[str, int]:
    """Inner loop — one IV-MTR pull per in-flight IMb, store new scans."""
    ivmtr = IVMTRClient(settings, conn, http_client)
    new_count = 0
    error_count = 0
    for imb in in_flight:
        try:
            resp = await ivmtr.get_tracking(imb)
        except Exception as exc:
            log.warning("poll: get_tracking failed for %s: %s", imb, exc)
            error_count += 1
            continue

        if resp.error is not None or resp.data is None:
            continue

        for scan in resp.data.scans:
            event_id = _synth_event_id(imb, scan)
            is_new = db.store_scan_event(
                conn,
                event_id=event_id,
                imb=imb,
                event_json=scan.model_dump_json().encode("utf-8"),
                scan_datetime=scan.scanDatetime.isoformat(),
            )
            if is_new:
                new_count += 1
    return {"polled": len(in_flight), "new_events": new_count, "errors": error_count}


def main() -> int:
    """Console entrypoint. Returns a Unix exit code (0 ok, 1 failure)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    try:
        result = asyncio.run(_poll_once(settings))
    except Exception:
        log.exception("poll pass failed")
        return 1
    log.info("poll: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
