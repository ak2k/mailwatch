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
from pathlib import Path
from typing import Any

SCHEMA = """\
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS serial_counters (
    day_bucket INTEGER PRIMARY KEY,
    counter    INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

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


def next_serial(conn: sqlite3.Connection, day_bucket: int) -> int:
    """Atomically increment and return the counter for ``day_bucket``.

    Uses a single ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` so the
    read-modify-write is a single statement under SQLite's write lock — no
    TOCTOU between concurrent callers.
    """
    row = conn.execute(
        "INSERT INTO serial_counters (day_bucket, counter) VALUES (?, 1) "
        "ON CONFLICT(day_bucket) DO UPDATE SET "
        "counter = counter + 1, updated_at = unixepoch() "
        "RETURNING counter",
        (day_bucket,),
    ).fetchone()
    counter: int = row[0]
    return counter


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
    serial_counters_ttl_hours: int = 48,
    address_cache_ttl_days: int = 365,
) -> dict[str, int]:
    """Delete rows past their TTLs and checkpoint the WAL.

    Never runs ``VACUUM`` — that would rewrite the DB and invalidate the
    Litestream generation, forcing a full re-snapshot. ``PRAGMA
    wal_checkpoint(TRUNCATE)`` is safe and keeps the WAL bounded.

    Returns a dict of ``{table: rows_deleted}``.
    """
    scan_cur = conn.execute(
        "DELETE FROM scan_events WHERE created_at < unixepoch() - ? * 86400",
        (scan_events_ttl_days,),
    )
    serial_cur = conn.execute(
        "DELETE FROM serial_counters WHERE updated_at < unixepoch() - ? * 3600",
        (serial_counters_ttl_hours,),
    )
    cache_cur = conn.execute(
        "DELETE FROM address_cache WHERE cached_at < unixepoch() - ? * 86400",
        (address_cache_ttl_days,),
    )
    deleted = {
        "scan_events": scan_cur.rowcount,
        "serial_counters": serial_cur.rowcount,
        "address_cache": cache_cur.rowcount,
    }
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted
