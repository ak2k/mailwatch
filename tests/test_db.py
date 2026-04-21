"""Tests for mailwatch.db — schema, pragmas, K/V, serial counter, idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mailwatch import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Per-test on-disk DB (not :memory:) so WAL is exercised like production."""
    c = db.connect(tmp_path / "mailwatch.db")
    db.init_db(c)
    return c


# --- pragmas + init ---------------------------------------------------------


def test_connect_sets_pragmas(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "pragma.db")

    journal_mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.lower() == "wal"

    synchronous = c.execute("PRAGMA synchronous").fetchone()[0]
    assert synchronous == 1  # NORMAL

    busy_timeout = c.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout == 5000

    foreign_keys = c.execute("PRAGMA foreign_keys").fetchone()[0]
    assert foreign_keys == 1


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "idem.db")
    db.init_db(c)
    db.init_db(c)  # must not raise
    # All four tables present
    tables = {
        row[0]
        for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }
    assert {"app_state", "serial_counters", "scan_events", "address_cache"} <= tables


# --- app_state K/V ----------------------------------------------------------


def test_state_roundtrip_bytes(conn: sqlite3.Connection) -> None:
    db.set_state(conn, "blob_key", b"\x00\x01\x02raw")
    assert db.get_state(conn, "blob_key") == b"\x00\x01\x02raw"


def test_state_roundtrip_str_encoded_utf8(conn: sqlite3.Connection) -> None:
    db.set_state(conn, "str_key", "hellø")
    assert db.get_state(conn, "str_key") == "hellø".encode()


def test_state_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_state(conn, "does_not_exist") is None


def test_state_upsert_overwrites(conn: sqlite3.Connection) -> None:
    db.set_state(conn, "k", b"first")
    db.set_state(conn, "k", b"second")
    assert db.get_state(conn, "k") == b"second"


# --- serial counter ---------------------------------------------------------


def test_next_serial_starts_at_one_and_increments(conn: sqlite3.Connection) -> None:
    assert db.next_serial(conn, 7) == 1
    assert db.next_serial(conn, 7) == 2
    assert db.next_serial(conn, 7) == 3


def test_next_serial_is_per_bucket(conn: sqlite3.Connection) -> None:
    assert db.next_serial(conn, 1) == 1
    assert db.next_serial(conn, 2) == 1
    assert db.next_serial(conn, 1) == 2
    assert db.next_serial(conn, 2) == 2
    assert db.next_serial(conn, 3) == 1


# --- scan events ------------------------------------------------------------


def test_store_scan_event_new_vs_duplicate(conn: sqlite3.Connection) -> None:
    assert (
        db.store_scan_event(
            conn, "evt-1", "imb-123", '{"state":"IN_TRANSIT"}', "2026-04-21T10:00:00Z"
        )
        is True
    )
    assert (
        db.store_scan_event(
            conn, "evt-1", "imb-123", '{"state":"DELIVERED"}', "2026-04-21T18:00:00Z"
        )
        is False
    )
    # Original event preserved, not overwritten
    events = db.get_scan_events(conn, "imb-123")
    assert len(events) == 1
    assert events[0]["event"] == {"state": "IN_TRANSIT"}


def test_get_scan_events_newest_first(conn: sqlite3.Connection) -> None:
    # Insert out-of-order; newer created_at must come first.
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("old", "imb-x", b'{"n":1}', "2026-04-20T09:00:00Z", 1_000),
    )
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("new", "imb-x", b'{"n":2}', "2026-04-21T09:00:00Z", 2_000),
    )
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("middle", "imb-x", b'{"n":3}', "2026-04-20T18:00:00Z", 1_500),
    )

    events = db.get_scan_events(conn, "imb-x")
    assert [e["event_id"] for e in events] == ["new", "middle", "old"]
    # Payload decoded
    assert events[0]["event"] == {"n": 2}


def test_get_scan_events_empty(conn: sqlite3.Connection) -> None:
    assert db.get_scan_events(conn, "no-such-imb") == []


# --- address cache ----------------------------------------------------------


def test_cache_roundtrip(conn: sqlite3.Connection) -> None:
    db.cache_put(conn, "hash1", b'{"zip":"10001"}')
    assert db.cache_get(conn, "hash1") == b'{"zip":"10001"}'


def test_cache_put_str_encoded(conn: sqlite3.Connection) -> None:
    db.cache_put(conn, "hash2", '{"zip":"94103"}')
    assert db.cache_get(conn, "hash2") == b'{"zip":"94103"}'


def test_cache_miss_returns_none(conn: sqlite3.Connection) -> None:
    assert db.cache_get(conn, "unknown") is None


def test_cache_put_overwrites(conn: sqlite3.Connection) -> None:
    db.cache_put(conn, "h", b"v1")
    db.cache_put(conn, "h", b"v2")
    assert db.cache_get(conn, "h") == b"v2"


def test_canonical_address_hash_normalisation() -> None:
    # The private helper is exercised to prove case/whitespace normalisation
    # actually collapses cosmetic differences — guards against API drift.
    h1 = db._canonical_address_hash({"city": "New York", "zip": "10001", "street": "123 Main St"})
    h2 = db._canonical_address_hash({"zip": "10001", "street": " 123 MAIN ST ", "city": "NEW YORK"})
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


# --- purge ------------------------------------------------------------------


def test_purge_old_respects_ttls(conn: sqlite3.Connection) -> None:
    # scan_events: one fresh (created_at=now), one stale (70 days old)
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('fresh', 'imb1', '{}', NULL, unixepoch())"
    )
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('stale', 'imb1', '{}', NULL, unixepoch() - 70 * 86400)"
    )
    # serial_counters: fresh + stale
    conn.execute(
        "INSERT INTO serial_counters (day_bucket, counter, updated_at) "
        "VALUES (10, 5, unixepoch())"
    )
    conn.execute(
        "INSERT INTO serial_counters (day_bucket, counter, updated_at) "
        "VALUES (11, 5, unixepoch() - 72 * 3600)"
    )
    # address_cache: fresh + stale (400 days > 365 default)
    conn.execute(
        "INSERT INTO address_cache (input_hash, response_json, cached_at) "
        "VALUES ('fresh', '{}', unixepoch())"
    )
    conn.execute(
        "INSERT INTO address_cache (input_hash, response_json, cached_at) "
        "VALUES ('stale', '{}', unixepoch() - 400 * 86400)"
    )

    deleted = db.purge_old(conn)
    assert deleted == {"scan_events": 1, "serial_counters": 1, "address_cache": 1}

    # Fresh rows survive
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM serial_counters").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM address_cache").fetchone()[0] == 1
    # Specifically the stale ones went
    assert conn.execute("SELECT event_id FROM scan_events").fetchone()[0] == "fresh"
    assert conn.execute("SELECT day_bucket FROM serial_counters").fetchone()[0] == 10
    assert conn.execute("SELECT input_hash FROM address_cache").fetchone()[0] == "fresh"


def test_purge_old_on_empty_tables(conn: sqlite3.Connection) -> None:
    deleted = db.purge_old(conn)
    assert deleted == {"scan_events": 0, "serial_counters": 0, "address_cache": 0}


# --- in-flight IMbs ---------------------------------------------------------


def _insert_scan(
    conn: sqlite3.Connection,
    event_id: str,
    imb: str,
    event_json: bytes,
    age_seconds: int,
) -> None:
    """Helper: insert a scan_events row with ``created_at = now - age_seconds``."""
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES (?, ?, ?, NULL, unixepoch() - ?)",
        (event_id, imb, event_json, age_seconds),
    )


def test_in_flight_empty_db(conn: sqlite3.Connection) -> None:
    assert db.get_in_flight_imbs(conn) == []


def test_in_flight_one_recent_non_delivered(conn: sqlite3.Connection) -> None:
    _insert_scan(conn, "e1", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == ["imb-A"]


def test_in_flight_delivered_is_excluded(conn: sqlite3.Connection) -> None:
    _insert_scan(conn, "e1", "imb-A", b'{"scanEventCode":"01"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == []


def test_in_flight_stale_outside_window(conn: sqlite3.Connection) -> None:
    # 30 days old is outside the default 14-day window.
    _insert_scan(conn, "e1", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=30 * 86400)
    assert db.get_in_flight_imbs(conn) == []


def test_in_flight_honours_lookback_days(conn: sqlite3.Connection) -> None:
    # 10 days old: outside a 7-day window but inside the default 14-day window.
    _insert_scan(conn, "e1", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=10 * 86400)
    assert db.get_in_flight_imbs(conn, lookback_days=7) == []
    assert db.get_in_flight_imbs(conn, lookback_days=14) == ["imb-A"]


def test_in_flight_latest_event_is_authoritative(conn: sqlite3.Connection) -> None:
    # Older in-transit scan; newer delivery scan → IMb is delivered, exclude.
    _insert_scan(conn, "old", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=2 * 86400)
    _insert_scan(conn, "new", "imb-A", b'{"scanEventCode":"01"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == []


def test_in_flight_latest_non_delivered_wins_over_old_delivered(
    conn: sqlite3.Connection,
) -> None:
    # Weird case (shouldn't happen in practice), but proves "latest" drives the
    # decision: an old "delivered" row followed by a newer "in transit" row
    # means the IMb is treated as still in flight.
    _insert_scan(conn, "old", "imb-A", b'{"scanEventCode":"01"}', age_seconds=2 * 86400)
    _insert_scan(conn, "new", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == ["imb-A"]


def test_in_flight_mixed_imbs(conn: sqlite3.Connection) -> None:
    _insert_scan(conn, "a1", "imb-A", b'{"scanEventCode":"SD"}', age_seconds=60)
    _insert_scan(conn, "b1", "imb-B", b'{"scanEventCode":"01"}', age_seconds=60)
    _insert_scan(conn, "c1", "imb-C", b'{"scanEventCode":"SD"}', age_seconds=30 * 86400)
    result = set(db.get_in_flight_imbs(conn))
    assert result == {"imb-A"}


def test_in_flight_malformed_payload_treated_as_non_delivery(
    conn: sqlite3.Connection,
) -> None:
    _insert_scan(conn, "e1", "imb-A", b"not-json", age_seconds=60)
    assert db.get_in_flight_imbs(conn) == ["imb-A"]


def test_in_flight_payload_without_scan_code_treated_as_non_delivery(
    conn: sqlite3.Connection,
) -> None:
    _insert_scan(conn, "e1", "imb-A", b'{"foo":"bar"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == ["imb-A"]


def test_in_flight_snake_case_scan_code_also_recognised(conn: sqlite3.Connection) -> None:
    _insert_scan(conn, "e1", "imb-A", b'{"scan_event_code":"01"}', age_seconds=60)
    assert db.get_in_flight_imbs(conn) == []


def test_purge_old_honors_custom_ttls(conn: sqlite3.Connection) -> None:
    # A 10-day-old scan event is NOT stale at 60d default but IS stale at 5d.
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('ten_day', 'imb1', '{}', NULL, unixepoch() - 10 * 86400)"
    )
    deleted_default = db.purge_old(conn)
    assert deleted_default["scan_events"] == 0
    deleted_tight = db.purge_old(conn, scan_events_ttl_days=5)
    assert deleted_tight["scan_events"] == 1
