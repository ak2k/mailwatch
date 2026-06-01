"""SQLite storage layer for mailwatch.

Stdlib `sqlite3` only — no ORM, no async wrapper. All functions are sync;
FastAPI handlers wrap blocking calls in ``asyncio.to_thread``.

Connections are passed explicitly; no module-level globals. This mirrors
the haystack pattern so the whole state is one file that Litestream can
continuously replicate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# USPS IV-MTR scan event codes treated as "delivered" for poll-gating purposes.
# Conservative list — if we miss one, the worst case is polling a delivered
# letter once per 30 min (cheap). Source: USPS IV-MTR MPE scan code reference.
# "01" is the long-standing "delivered" code for letter mail; additional codes
# can be added here without schema migration.
DELIVERED_SCAN_CODES: frozenset[str] = frozenset({"01"})

SCHEMA = """\
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS serial_state (
    id      INTEGER PRIMARY KEY CHECK (id = 0),
    counter INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_imbs (
    imb           TEXT PRIMARY KEY,
    serial        INTEGER NOT NULL,
    recipient_zip TEXT,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS tracked_imbs_serial_idx ON tracked_imbs(serial);

CREATE TABLE IF NOT EXISTS scan_events (
    event_id      TEXT PRIMARY KEY,
    imb           TEXT NOT NULL,
    event_json    BLOB NOT NULL,
    scan_datetime TEXT,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS scan_events_imb_idx ON scan_events(imb);
CREATE INDEX IF NOT EXISTS scan_events_created_idx ON scan_events(created_at);

CREATE TABLE IF NOT EXISTS address_cache (
    input_hash    TEXT PRIMARY KEY,
    response_json BLOB NOT NULL,
    cached_at     INTEGER NOT NULL DEFAULT (unixepoch())
);
"""

# Floor for the global monotonic serial counter. The serial field in an IMb is
# only 6 digits when the Mailer ID is 9 digits (and 9 digits when the MID is 6),
# so an IMb's tracking identity must stay unique for the full IV-MTR retention
# window (~months). Earlier builds reset the counter per UTC day and encoded no
# date into the IMb, so serial 1 on two different days produced *identical*
# barcodes. The fix is a single ever-increasing counter that is never reset or
# purged. The floor starts allocation safely above any low serial issued under
# the old per-day scheme (historical daily volume was a handful), so a fresh
# global counter can never collide with a letter already in the mail stream.
SERIAL_FLOOR: int = 1000


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with sensible pragmas.

    - ``journal_mode=WAL`` — cooperates with Litestream and concurrent readers.
    - ``busy_timeout=5000`` — tolerate brief writer contention.
    - ``synchronous=NORMAL`` — the WAL-recommended durability/perf tradeoff.
    - ``foreign_keys=ON`` — enforce declared FKs (future-proofing).
    - ``check_same_thread=False`` — callers wrap in ``asyncio.to_thread``.
    - ``isolation_level=None`` — autocommit; writes use explicit transactions.
    """
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        isolation_level=None,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't exist. Idempotent."""
    conn.executescript(SCHEMA)


# --- app_state K/V -----------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str) -> bytes | None:
    """Return raw bytes for ``app_state[key]``, or None if absent."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value: bytes = row[0]
    return value


def set_state(conn: sqlite3.Connection, key: str, value: bytes | str) -> None:
    """Upsert ``app_state[key] = value``. ``str`` inputs are UTF-8 encoded."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, updated_at = unixepoch()",
        (key, value),
    )


# --- serial counters ---------------------------------------------------------


def next_serial(conn: sqlite3.Connection, floor: int = SERIAL_FLOOR) -> int:
    """Allocate the next single global monotonic serial.

    Thin wrapper over :func:`next_serials` for the common single-letter case.
    """
    return next_serials(conn, 1, floor=floor)[0]


def next_serials(conn: sqlite3.Connection, count: int, floor: int = SERIAL_FLOOR) -> list[int]:
    """Atomically allocate ``count`` consecutive global serials.

    A single ``serial_state`` row (``id = 0``) holds an ever-increasing
    counter that is **never reset and never purged** — this is what makes
    every minted IMb globally unique (see :data:`SERIAL_FLOOR`). The counter
    is seeded to ``floor`` on first use, so the first serial returned is
    ``floor + 1``.

    Allocation runs inside a ``BEGIN IMMEDIATE`` transaction so the
    seed-then-increment is atomic under SQLite's write lock; two concurrent
    callers therefore never receive overlapping ranges.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO serial_state (id, counter) VALUES (0, ?)",
            (floor,),
        )
        row = conn.execute(
            "UPDATE serial_state SET counter = counter + ? WHERE id = 0 RETURNING counter",
            (count,),
        ).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    last: int = row[0]
    first = last - count + 1
    return list(range(first, last + 1))


# --- tracked IMb registry ----------------------------------------------------


def register_imb(
    conn: sqlite3.Connection,
    imb: str,
    serial: int,
    recipient_zip: str | None = None,
) -> bool:
    """Record a generated IMb so the poller can track it without a prior scan.

    The stored ``imb`` is the *full* queryable barcode (tracking digits +
    routing code) — the exact string IV-MTR keys on. This is what lets
    :func:`get_imb_by_serial` reconstruct the precise IMb for ``/track-ws``
    (a typed ZIP can't reproduce the encoded 11-digit routing) and lets
    :func:`get_pollable_imbs` bootstrap a freshly mailed letter that has no
    scan event yet.

    Idempotent via ``INSERT OR IGNORE`` on the IMb primary key. Returns True
    when a new row was inserted.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO tracked_imbs (imb, serial, recipient_zip) VALUES (?, ?, ?)",
        (imb, serial, recipient_zip),
    )
    return cur.rowcount > 0


def get_imb_by_serial(conn: sqlite3.Connection, serial: int) -> str | None:
    """Return the full registered IMb for ``serial``, or None if unregistered.

    Serials are globally unique (see :func:`next_serials`), so at most one row
    matches; the ordering is a defensive tiebreak only.
    """
    row = conn.execute(
        "SELECT imb FROM tracked_imbs WHERE serial = ? ORDER BY created_at DESC LIMIT 1",
        (serial,),
    ).fetchone()
    return row[0] if row else None


# --- scan events -------------------------------------------------------------


def store_scan_event(
    conn: sqlite3.Connection,
    event_id: str,
    imb: str,
    event_json: bytes | str,
    scan_datetime: str | None,
) -> bool:
    """Insert a scan event idempotently keyed on ``event_id``.

    Returns True if the row was newly inserted, False on duplicate.
    """
    if isinstance(event_json, str):
        event_json = event_json.encode("utf-8")
    cur = conn.execute(
        "INSERT OR IGNORE INTO scan_events "
        "(event_id, imb, event_json, scan_datetime) VALUES (?, ?, ?, ?)",
        (event_id, imb, event_json, scan_datetime),
    )
    return cur.rowcount > 0


def get_scan_events(conn: sqlite3.Connection, imb: str) -> list[dict[str, Any]]:
    """Return all scan events for a given IMb, newest first.

    ``event_json`` is decoded from the stored blob; callers get a ready-to-use
    dict list.
    """
    rows = conn.execute(
        "SELECT event_id, imb, event_json, scan_datetime, created_at "
        "FROM scan_events WHERE imb = ? "
        "ORDER BY created_at DESC, event_id DESC",
        (imb,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for event_id, row_imb, event_json, scan_datetime, created_at in rows:
        try:
            payload = json.loads(event_json)
        except (json.JSONDecodeError, TypeError):
            payload = None
        result.append(
            {
                "event_id": event_id,
                "imb": row_imb,
                "event": payload,
                "scan_datetime": scan_datetime,
                "created_at": created_at,
            }
        )
    return result


def get_in_flight_imbs(conn: sqlite3.Connection, lookback_days: int = 14) -> list[str]:
    """Return distinct IMbs with a scan_event in the last ``lookback_days``.

    Excludes IMbs whose *latest* scan (by ``created_at``, tiebreaking on
    ``event_id`` for determinism) indicates delivery — specifically, whose
    latest event's ``event_json`` decodes to a dict with a ``scanEventCode``
    (or ``scan_event_code``) in :data:`DELIVERED_SCAN_CODES`.

    If decoding fails or no scan code is present the event is treated as
    non-delivery (conservative: we'd rather over-poll than drop a letter).

    The SQL groups the recent window by IMb and uses a correlated subquery to
    pick out the per-IMb latest row; delivery-code filtering happens in Python
    because ``event_json`` is an opaque BLOB and we don't want to trust SQLite
    JSON1 being present on every build.
    """
    cutoff = int(time.time()) - lookback_days * 86400
    rows = conn.execute(
        """
        SELECT se.imb, se.event_json
        FROM scan_events se
        WHERE se.created_at >= ?
          AND se.created_at = (
              SELECT MAX(s2.created_at)
              FROM scan_events s2
              WHERE s2.imb = se.imb
          )
          AND se.event_id = (
              SELECT MAX(s3.event_id)
              FROM scan_events s3
              WHERE s3.imb = se.imb
                AND s3.created_at = se.created_at
          )
        """,
        (cutoff,),
    ).fetchall()

    result: list[str] = []
    for imb, event_json in rows:
        if _is_delivered_payload(event_json):
            continue
        result.append(imb)
    return result


def get_pollable_imbs(
    conn: sqlite3.Connection,
    lookback_days: int = 14,
    max_age_days: int = 45,
) -> list[str]:
    """Return every IMb the poller should query this pass.

    The union of two sources:

    1. **Scan-driven** — IMbs with a recent non-delivery scan
       (:func:`get_in_flight_imbs`). Covers push-fed pieces and anything
       seen only via the webhook.
    2. **Registry-driven** — IMbs from :func:`register_imb` minted within
       ``max_age_days`` whose latest scan (if any) isn't a delivery code.
       This is what lets a *brand-new* letter be polled before its first
       scan exists — the scan-driven source alone can never bootstrap one.

    ``max_age_days`` bounds how long we keep polling a registered IMb that
    never produces a delivery scan, so the poll set can't grow without
    bound. Delivered pieces drop out of both sources.
    """
    pollable = set(get_in_flight_imbs(conn, lookback_days))
    cutoff = int(time.time()) - max_age_days * 86400
    rows = conn.execute(
        "SELECT imb FROM tracked_imbs WHERE created_at >= ?",
        (cutoff,),
    ).fetchall()
    for (imb,) in rows:
        latest = conn.execute(
            "SELECT event_json FROM scan_events WHERE imb = ? "
            "ORDER BY created_at DESC, event_id DESC LIMIT 1",
            (imb,),
        ).fetchone()
        if latest is not None and _is_delivered_payload(latest[0]):
            continue
        pollable.add(imb)
    return sorted(pollable)


def _is_delivered_payload(event_json: bytes | str | None) -> bool:
    """Return True if the decoded scan payload indicates delivery."""
    if not event_json:
        return False
    try:
        payload = json.loads(event_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    code = payload.get("scanEventCode") or payload.get("scan_event_code")
    if not isinstance(code, str):
        return False
    return code in DELIVERED_SCAN_CODES


# --- address cache -----------------------------------------------------------


def _canonical_address_hash(payload: dict[str, Any]) -> str:
    """Return sha256 hex digest of a JSON-canonicalized address dict.

    Normalisation: keys sorted, no whitespace, UTF-8. String values are
    stripped and upper-cased so that cosmetic differences (`"new york"` vs
    `" New York "`) hash identically. This is a module-private helper; the
    public cache API takes an already-computed digest.
    """
    normalized: dict[str, Any] = {}
    for k in sorted(payload):
        v = payload[k]
        if isinstance(v, str):
            v = v.strip().upper()
        normalized[k] = v
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def hash_address_dict(d: dict[str, Any]) -> str:
    """Return the canonical cache-key hash for an address-request dict.

    Public wrapper around :func:`_canonical_address_hash` so clients can
    reuse the same normalisation (sort keys, strip + upper-case strings)
    without importing a private helper.
    """
    return _canonical_address_hash(d)


def cache_get(conn: sqlite3.Connection, input_hash: str) -> bytes | None:
    """Return cached response bytes for ``input_hash``, or None."""
    row = conn.execute(
        "SELECT response_json FROM address_cache WHERE input_hash = ?",
        (input_hash,),
    ).fetchone()
    if row is None:
        return None
    value: bytes = row[0]
    return value


def cache_put(conn: sqlite3.Connection, input_hash: str, response_json: bytes | str) -> None:
    """Upsert an address-cache entry. ``str`` is UTF-8 encoded."""
    if isinstance(response_json, str):
        response_json = response_json.encode("utf-8")
    conn.execute(
        "INSERT INTO address_cache (input_hash, response_json) VALUES (?, ?) "
        "ON CONFLICT(input_hash) DO UPDATE SET "
        "response_json = excluded.response_json, cached_at = unixepoch()",
        (input_hash, response_json),
    )


# --- cleanup -----------------------------------------------------------------


def purge_old(
    conn: sqlite3.Connection,
    scan_events_ttl_days: int = 60,
    address_cache_ttl_days: int = 365,
) -> dict[str, int]:
    """Delete rows past their TTLs.

    Note: ``serial_state`` is deliberately never purged — the global serial
    counter must keep climbing forever to guarantee IMb uniqueness (see
    :func:`next_serials`). ``tracked_imbs`` is likewise retained; it's tiny
    (one row per letter) and feeds long-tail tracking lookups.

    Intentionally does not run ``VACUUM`` (that would rewrite the DB and
    invalidate the Litestream generation) or ``PRAGMA wal_checkpoint`` of
    any mode. Litestream holds a read lock specifically to prevent
    application-side checkpoints; app-level TRUNCATE either gets blocked
    by that lock or races with Litestream's own checkpoint state and
    causes the replica to lose WAL position (tracked upstream in
    Litestream issues #521, #547, #58). Litestream 0.5.x manages WAL
    growth itself via `min-checkpoint-page-count` and `truncate-page-n`;
    leaving it alone is the documented best practice.

    Returns a dict of ``{table: rows_deleted}``.
    """
    scan_cur = conn.execute(
        "DELETE FROM scan_events WHERE created_at < unixepoch() - ? * 86400",
        (scan_events_ttl_days,),
    )
    cache_cur = conn.execute(
        "DELETE FROM address_cache WHERE cached_at < unixepoch() - ? * 86400",
        (address_cache_ttl_days,),
    )
    return {
        "scan_events": scan_cur.rowcount,
        "address_cache": cache_cur.rowcount,
    }
