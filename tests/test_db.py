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
    assert {
        "app_state",
        "serial_state",
        "tracked_imbs",
        "scan_events",
        "address_cache",
    } <= tables


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


# --- serial counter (global monotonic) --------------------------------------


def test_next_serial_starts_above_floor_and_increments(conn: sqlite3.Connection) -> None:
    # First serial is floor+1; the counter is global and never resets.
    assert db.next_serial(conn) == db.SERIAL_FLOOR + 1
    assert db.next_serial(conn) == db.SERIAL_FLOOR + 2
    assert db.next_serial(conn) == db.SERIAL_FLOOR + 3


def test_next_serial_is_globally_monotonic_not_per_day(conn: sqlite3.Connection) -> None:
    # There is exactly one counter — no per-bucket reset. Two "days" worth of
    # allocations keep climbing, so no two pieces ever share a serial (and thus
    # an IMb).
    first = db.next_serial(conn)
    second = db.next_serial(conn)
    third = db.next_serial(conn)
    assert first < second < third
    assert len({first, second, third}) == 3


def test_next_serials_allocates_contiguous_range(conn: sqlite3.Connection) -> None:
    batch = db.next_serials(conn, 4)
    assert batch == [
        db.SERIAL_FLOOR + 1,
        db.SERIAL_FLOOR + 2,
        db.SERIAL_FLOOR + 3,
        db.SERIAL_FLOOR + 4,
    ]
    # The next single serial continues past the batch — no overlap.
    assert db.next_serial(conn) == db.SERIAL_FLOOR + 5


def test_next_serials_respects_custom_floor(conn: sqlite3.Connection) -> None:
    # The floor only seeds the very first allocation; later calls ignore it.
    assert db.next_serials(conn, 1, floor=50_000) == [50_001]
    assert db.next_serial(conn, floor=50_000) == 50_002


def test_next_serials_rejects_non_positive_count(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        db.next_serials(conn, 0)


# --- tracked IMb registry ---------------------------------------------------


def test_register_imb_and_lookup_by_serial(conn: sqlite3.Connection) -> None:
    full_imb = "0" * 31
    assert db.register_imb(conn, full_imb, 1001, "10009") is True
    # Idempotent: re-registering the same IMb is a no-op.
    assert db.register_imb(conn, full_imb, 1001, "10009") is False
    assert db.get_imb_by_serial(conn, 1001) == full_imb


def test_get_imb_by_serial_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_imb_by_serial(conn, 9999) is None


def test_pollable_includes_registered_imb_without_scans(conn: sqlite3.Connection) -> None:
    # A freshly registered letter with no scan event must still be pollable —
    # this is the bootstrap case the scan-only query could never cover.
    db.register_imb(conn, "imb-fresh", 1001, "10009")
    assert db.get_pollable_imbs(conn) == ["imb-fresh"]


def test_pollable_excludes_delivered_registered_imb(conn: sqlite3.Connection) -> None:
    db.register_imb(conn, "imb-done", 1001, "10009")
    _insert_scan(conn, "e1", "imb-done", b'{"scanEventCode":"01"}', age_seconds=60)
    assert db.get_pollable_imbs(conn) == []


def test_pollable_excludes_registered_imb_past_max_age(conn: sqlite3.Connection) -> None:
    # Registered 60 days ago, never delivered → outside the 45-day poll window.
    conn.execute(
        "INSERT INTO tracked_imbs (imb, serial, recipient_zip, created_at) "
        "VALUES ('imb-stale', 1001, '10009', unixepoch() - 60 * 86400)"
    )
    assert db.get_pollable_imbs(conn) == []


def test_pollable_unions_scan_and_registry_sources(conn: sqlite3.Connection) -> None:
    # Scan-only IMb (e.g. push-fed, never registered) + a registry-only IMb.
    _insert_scan(conn, "e1", "imb-scan", b'{"scanEventCode":"SD"}', age_seconds=60)
    db.register_imb(conn, "imb-reg", 1001, "10009")
    assert set(db.get_pollable_imbs(conn)) == {"imb-scan", "imb-reg"}


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
    assert deleted == {"scan_events": 1, "address_cache": 1}

    # Fresh rows survive
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM address_cache").fetchone()[0] == 1
    # Specifically the stale ones went
    assert conn.execute("SELECT event_id FROM scan_events").fetchone()[0] == "fresh"
    assert conn.execute("SELECT input_hash FROM address_cache").fetchone()[0] == "fresh"


def test_purge_old_never_touches_serial_state(conn: sqlite3.Connection) -> None:
    # The global serial counter must survive cleanup forever — purging it would
    # reset serials and risk reusing an IMb already in the mail stream.
    db.next_serials(conn, 3)  # seed + advance the counter
    before = conn.execute("SELECT counter FROM serial_state WHERE id = 0").fetchone()[0]
    db.purge_old(conn)
    after = conn.execute("SELECT counter FROM serial_state WHERE id = 0").fetchone()[0]
    assert after == before


def test_purge_old_on_empty_tables(conn: sqlite3.Connection) -> None:
    deleted = db.purge_old(conn)
    assert deleted == {"scan_events": 0, "address_cache": 0}


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


# --- recent tracked IMbs (tracking-page list) -------------------------------


def test_recent_tracked_imbs_empty_db(conn: sqlite3.Connection) -> None:
    assert db.recent_tracked_imbs(conn) == []


def test_recent_tracked_imbs_newest_first_and_status(conn: sqlite3.Connection) -> None:
    # Three letters with increasing created_at; expect newest first.
    conn.execute(
        "INSERT INTO tracked_imbs (imb, serial, recipient_zip, created_at) VALUES "
        "('imb-old', 1001, '10009', 1000),"
        "('imb-mid', 1002, '20002', 2000),"
        "('imb-new', 1003, '30003', 3000)"
    )
    # imb-old delivered, imb-mid in transit, imb-new no scans yet.
    _insert_scan(conn, "e-old", "imb-old", b'{"scanEventCode":"01"}', age_seconds=60)
    _insert_scan(conn, "e-mid", "imb-mid", b'{"scanEventCode":"SD"}', age_seconds=60)

    rows = db.recent_tracked_imbs(conn)
    assert [r["serial"] for r in rows] == [1003, 1002, 1001]
    by_serial = {r["serial"]: r for r in rows}
    assert by_serial[1003]["status"] == "awaiting"
    assert by_serial[1002]["status"] == "in_transit"
    assert by_serial[1001]["status"] == "delivered"
    assert by_serial[1003]["recipient_zip"] == "30003"


def test_recent_tracked_imbs_status_uses_latest_scan(conn: sqlite3.Connection) -> None:
    # An earlier delivery-looking-but-not code followed by a later delivery
    # code must report "delivered"; ordering is by created_at then event_id.
    db.register_imb(conn, "imb-x", 1001, "10009")
    _insert_scan(conn, "e1", "imb-x", b'{"scanEventCode":"SD"}', age_seconds=120)
    _insert_scan(conn, "e2", "imb-x", b'{"scanEventCode":"01"}', age_seconds=10)
    rows = db.recent_tracked_imbs(conn)
    assert rows[0]["status"] == "delivered"


def test_recent_tracked_imbs_honours_limit(conn: sqlite3.Connection) -> None:
    for i in range(5):
        conn.execute(
            "INSERT INTO tracked_imbs (imb, serial, recipient_zip, created_at) "
            "VALUES (?, ?, ?, ?)",
            (f"imb-{i}", 1000 + i, "10009", 1000 + i),
        )
    rows = db.recent_tracked_imbs(conn, limit=2)
    assert [r["serial"] for r in rows] == [1004, 1003]


def test_recent_tracked_imbs_omits_null_zip(conn: sqlite3.Connection) -> None:
    # A null recipient_zip can't form a usable tracking link, so it's skipped.
    db.register_imb(conn, "imb-null", 1001, None)
    db.register_imb(conn, "imb-ok", 1002, "10009")
    rows = db.recent_tracked_imbs(conn)
    assert [r["serial"] for r in rows] == [1002]


def test_register_imb_persists_name_and_company(conn: sqlite3.Connection) -> None:
    db.register_imb(conn, "imb-x", 1001, "10009", "Jane Doe", "Acme Co")
    rows = db.recent_tracked_imbs(conn)
    assert rows[0]["recipient_name"] == "Jane Doe"
    assert rows[0]["recipient_company"] == "Acme Co"


def test_recent_tracked_imbs_name_company_default_none(conn: sqlite3.Connection) -> None:
    # Letters registered without the metadata (e.g. pre-migration) surface None.
    db.register_imb(conn, "imb-x", 1001, "10009")
    rows = db.recent_tracked_imbs(conn)
    assert rows[0]["recipient_name"] is None
    assert rows[0]["recipient_company"] is None


def test_migration_adds_name_company_to_legacy_table(tmp_path: Path) -> None:
    """init_db backfills the new columns on a DB created with the old schema."""
    c = db.connect(tmp_path / "legacy.db")
    # Recreate the pre-metadata tracked_imbs shape, then seed a row.
    c.executescript(
        "CREATE TABLE tracked_imbs ("
        "  imb TEXT PRIMARY KEY, serial INTEGER NOT NULL, recipient_zip TEXT,"
        "  created_at INTEGER NOT NULL DEFAULT (unixepoch()));"
    )
    c.execute(
        "INSERT INTO tracked_imbs (imb, serial, recipient_zip) VALUES ('imb-old', 1001, '10009')"
    )
    cols_before = {row[1] for row in c.execute("PRAGMA table_info(tracked_imbs)")}
    assert "recipient_name" not in cols_before

    db.init_db(c)  # idempotent create + migrate

    cols_after = {row[1] for row in c.execute("PRAGMA table_info(tracked_imbs)")}
    assert {"recipient_name", "recipient_company"} <= cols_after
    # Existing row preserved; new columns read as NULL.
    rows = db.recent_tracked_imbs(c)
    assert rows[0]["serial"] == 1001
    assert rows[0]["recipient_name"] is None
    # New writes can populate the columns.
    db.register_imb(c, "imb-new", 1002, "20002", "Jane Doe", "Acme Co")
    by_serial = {r["serial"]: r for r in db.recent_tracked_imbs(c)}
    assert by_serial[1002]["recipient_name"] == "Jane Doe"

    # Re-running the migration is a no-op (no error, columns unchanged).
    db.init_db(c)
    cols_again = {row[1] for row in c.execute("PRAGMA table_info(tracked_imbs)")}
    assert cols_again == cols_after
    c.close()
