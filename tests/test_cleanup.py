"""Tests for :mod:`mailwatch.cleanup`."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailwatch import cleanup, db
from mailwatch.config import get_settings


@pytest.fixture
def _settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every env var at a per-test tmp DB and clear the settings cache."""
    db_path = tmp_path / "mailwatch.db"
    monkeypatch.setenv("MAILER_ID", "123456")
    monkeypatch.setenv("BSG_USERNAME", "u")
    monkeypatch.setenv("BSG_PASSWORD", "p")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_ID", "c")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_SECRET", "s")
    monkeypatch.setenv("SESSION_KEY", "a" * 32)
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Defeat the lru_cache on get_settings for this test.
    get_settings.cache_clear()
    yield db_path
    get_settings.cache_clear()


def test_cleanup_returns_delete_counts(_settings_env: Path) -> None:
    """Running ``cleanup.main`` on a fresh DB creates tables and reports zeros."""
    assert not _settings_env.exists()
    deleted = cleanup.main()
    assert deleted == {"scan_events": 0, "serial_counters": 0, "address_cache": 0}
    assert _settings_env.exists()


def test_cleanup_removes_old_rows(_settings_env: Path) -> None:
    """Old rows are deleted; fresh rows survive."""
    conn = db.connect(_settings_env)
    db.init_db(conn)

    # Two scan events: one old (70 days), one fresh (today).
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('old', '1', x'7b7d', NULL, unixepoch() - 70 * 86400)"
    )
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('new', '1', x'7b7d', NULL, unixepoch() - 1 * 86400)"
    )
    conn.close()

    deleted = cleanup.main()
    assert deleted["scan_events"] == 1

    # Verify the fresh row survived.
    conn = db.connect(_settings_env)
    row = conn.execute("SELECT event_id FROM scan_events").fetchone()
    conn.close()
    assert row == ("new",)
