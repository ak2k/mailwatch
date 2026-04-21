"""Tests for :mod:`mailwatch.poll`.

Uses :class:`httpx.MockTransport` to intercept IV-MTR calls. Each test gets
a per-test tmp DB via env-var-driven :class:`Settings` so the one-shot script
entrypoint is exercised end-to-end without ever touching the network or the
real filesystem outside ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from mailwatch import db, poll
from mailwatch.config import Settings, get_settings
from mailwatch.models import TrackingScan
from mailwatch.usps_api import IV_TRACKING_URL

# --- shared fixtures -------------------------------------------------------- #


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Fresh Settings pointed at a per-test tmp DB, no env coupling."""
    db_path = tmp_path / "mailwatch.db"
    monkeypatch.setenv("MAILER_ID", "123456")
    monkeypatch.setenv("BSG_USERNAME", "bsg-user")
    monkeypatch.setenv("BSG_PASSWORD", "bsg-pw")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_ID", "c")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_SECRET", "s")
    monkeypatch.setenv("SESSION_KEY", "a" * 32)
    monkeypatch.setenv("DB_PATH", str(db_path))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse tenacity's exp-backoff sleeps so tests run in milliseconds."""

    async def _zero(_seconds: float) -> None:
        return None

    monkeypatch.setattr("tenacity.nap.sleep", _zero)


# --- helpers --------------------------------------------------------------- #


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Minimal request recorder backing :class:`httpx.MockTransport`."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._handlers: list[tuple[str, Callable[[str], bool], Handler]] = []

    def add(
        self,
        method: str,
        url_prefix: str,
        handler: Handler,
    ) -> None:
        def _match(u: str, p: str = url_prefix) -> bool:
            return u == p or u.startswith((p + "?", p + "/"))

        self._handlers.append((method, _match, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, pred, handler in self._handlers:
            if request.method == method and pred(url):
                return handler(request)
        raise AssertionError(f"Unregistered {request.method} {url}")


def _iv_auth_response(_req: httpx.Request) -> httpx.Response:
    """Canned IV-MTR authenticate response; tests use cached tokens by default."""
    return httpx.Response(
        200,
        json={
            "access_token": "iv-access",
            "refresh_token": "iv-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )


def _insert_in_flight(conn: sqlite3.Connection, imb: str) -> None:
    """Seed an in-transit scan so the IMb qualifies as "in flight"."""
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES (?, ?, ?, ?, unixepoch() - 3600)",
        (f"seed-{imb}", imb, b'{"scanEventCode":"SD"}', "2026-04-21T00:00:00+00:00"),
    )


def _prime_iv_token(conn: sqlite3.Connection) -> None:
    """Prepopulate IV-MTR token cache so the poll skips the auth POST."""
    import time

    db.set_state(conn, "iv_access_token", "iv-access")
    db.set_state(conn, "iv_refresh_token", "iv-refresh")
    db.set_state(conn, "iv_token_expiry", f"{time.time() + 600:.3f}")


def _make_tracking_body(imb: str, scans: list[dict[str, object]]) -> dict[str, object]:
    return {"data": {"imb": imb, "scans": scans}}


# --- tests ----------------------------------------------------------------- #


async def test_empty_db_no_network(settings: Settings) -> None:
    """No in-flight IMbs → no network calls, zeros returned."""
    recorder = Recorder()  # no handlers: any call would explode
    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    assert result == {"polled": 0, "new_events": 0, "errors": 0}
    assert recorder.calls == []


async def test_single_imb_stores_new_scans(settings: Settings) -> None:
    """One in-flight IMb → two scans fetched → two rows stored."""
    imb = "9" * 20
    # Seed the DB before the poll runs.
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb)
    _prime_iv_token(conn)
    conn.close()

    recorder = Recorder()
    recorder.add(
        "GET",
        IV_TRACKING_URL,
        lambda _r: httpx.Response(
            200,
            json=_make_tracking_body(
                imb,
                [
                    {
                        "imb": imb,
                        "scanDatetime": "2026-04-21T09:00:00+00:00",
                        "scanEventCode": "SD",
                    },
                    {
                        "imb": imb,
                        "scanDatetime": "2026-04-21T18:00:00+00:00",
                        "scanEventCode": "DP",
                    },
                ],
            ),
        ),
    )

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    assert result == {"polled": 1, "new_events": 2, "errors": 0}

    # Rows actually landed in the DB.
    conn = db.connect(settings.DB_PATH)
    events = db.get_scan_events(conn, imb)
    conn.close()
    scan_codes = {e["event"]["scanEventCode"] for e in events}
    assert scan_codes == {"SD", "DP"}


async def test_idempotent_second_run_stores_nothing(settings: Settings) -> None:
    """Running twice against the same mocked payload is a no-op on run 2."""
    imb = "1" * 20
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb)
    _prime_iv_token(conn)
    conn.close()

    body = _make_tracking_body(
        imb,
        [
            {
                "imb": imb,
                "scanDatetime": "2026-04-21T09:00:00+00:00",
                "scanEventCode": "SD",
            },
        ],
    )

    recorder = Recorder()
    recorder.add("GET", IV_TRACKING_URL, lambda _r: httpx.Response(200, json=body))

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        first = await poll._poll_once(settings, http_client=http)
        second = await poll._poll_once(settings, http_client=http)

    assert first["new_events"] == 1
    assert second == {"polled": 1, "new_events": 0, "errors": 0}


def test_synth_event_id_is_deterministic() -> None:
    """Same inputs → same digest; different inputs → different digest."""
    scan1 = TrackingScan(
        imb="9" * 20,
        scanDatetime=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        scanEventCode="SD",
    )
    scan2 = TrackingScan(
        imb="9" * 20,
        scanDatetime=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        scanEventCode="DP",
    )
    a = poll._synth_event_id("imb-A", scan1)
    b = poll._synth_event_id("imb-A", scan1)
    c = poll._synth_event_id("imb-A", scan2)
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex


async def test_push_and_pull_event_ids_do_not_collide(settings: Settings) -> None:
    """A push eventId and a pull synthetic id for the *same* scan are distinct.

    This is the intended design — the two storage paths write two rows for the
    same physical scan. Downstream UI merges on ``scan_datetime``. If we ever
    wanted true de-duplication we'd have to unify the id schemes.
    """
    imb = "7" * 20
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb)
    _prime_iv_token(conn)

    # Pre-seed a push-sourced row for the same scan using a USPS-style eventId.
    push_event_id = "push-12345"
    push_scan_dt = "2026-04-21T09:00:00+00:00"
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime) " "VALUES (?, ?, ?, ?)",
        (push_event_id, imb, b'{"source":"push","scanEventCode":"SD"}', push_scan_dt),
    )
    conn.close()

    recorder = Recorder()
    recorder.add(
        "GET",
        IV_TRACKING_URL,
        lambda _r: httpx.Response(
            200,
            json=_make_tracking_body(
                imb,
                [
                    {
                        "imb": imb,
                        "scanDatetime": push_scan_dt,
                        "scanEventCode": "SD",
                    }
                ],
            ),
        ),
    )

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    # The pull produced a new row because its synthetic id hashes differently
    # from the opaque USPS push eventId.
    assert result["new_events"] == 1

    conn = db.connect(settings.DB_PATH)
    rows = conn.execute(
        "SELECT event_id FROM scan_events WHERE imb = ? AND scan_datetime = ?",
        (imb, push_scan_dt),
    ).fetchall()
    conn.close()
    ids = {r[0] for r in rows}
    # Both rows exist — push-sourced and pull-sourced.
    assert push_event_id in ids
    assert len(ids) == 2


async def test_error_for_one_imb_does_not_abort_pass(settings: Settings) -> None:
    """One failing IMb increments ``errors`` but the other still produces scans."""
    imb_bad = "b" * 20
    imb_good = "g" * 20
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb_bad)
    _insert_in_flight(conn, imb_good)
    _prime_iv_token(conn)
    conn.close()

    def _tracking_handler(req: httpx.Request) -> httpx.Response:
        # Path suffix is the IMb we asked about.
        if str(req.url).endswith(imb_bad):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200,
            json=_make_tracking_body(
                imb_good,
                [
                    {
                        "imb": imb_good,
                        "scanDatetime": "2026-04-21T09:00:00+00:00",
                        "scanEventCode": "SD",
                    }
                ],
            ),
        )

    recorder = Recorder()
    recorder.add("GET", IV_TRACKING_URL, _tracking_handler)

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    assert result["polled"] == 2
    assert result["errors"] == 1
    assert result["new_events"] == 1


async def test_lookback_window_bounds_the_imb_set(settings: Settings) -> None:
    """Only IMbs with events inside the lookback window get polled."""
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    # Recent event (1h old) → in flight.
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('recent', 'imb-recent', ?, NULL, unixepoch() - 3600)",
        (b'{"scanEventCode":"SD"}',),
    )
    # Old event (30d old) → outside default 14d window.
    conn.execute(
        "INSERT INTO scan_events (event_id, imb, event_json, scan_datetime, created_at) "
        "VALUES ('stale', 'imb-stale', ?, NULL, unixepoch() - 30 * 86400)",
        (b'{"scanEventCode":"SD"}',),
    )
    _prime_iv_token(conn)
    conn.close()

    polled: list[str] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        polled.append(str(req.url).rsplit("/", 1)[-1])
        imb = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=_make_tracking_body(imb, []))

    recorder = Recorder()
    recorder.add("GET", IV_TRACKING_URL, _handler)

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    assert polled == ["imb-recent"]
    assert result == {"polled": 1, "new_events": 0, "errors": 0}


async def test_tracking_response_error_is_counted_as_zero_new(
    settings: Settings,
) -> None:
    """IV-MTR returns an error-only envelope → no new events, no exception."""
    imb = "e" * 20
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb)
    _prime_iv_token(conn)
    conn.close()

    recorder = Recorder()
    recorder.add(
        "GET",
        IV_TRACKING_URL,
        lambda _r: httpx.Response(200, json={"error": "no data"}),
    )

    transport = httpx.MockTransport(recorder)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await poll._poll_once(settings, http_client=http)

    assert result == {"polled": 1, "new_events": 0, "errors": 0}


async def test_poll_once_creates_its_own_client_when_none_provided(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default branch constructs an AsyncClient and closes it cleanly.

    We intercept ``httpx.AsyncClient`` so the constructed client is backed by
    a MockTransport (no sockets) regardless of what args poll passes in.
    """
    imb = "z" * 20
    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    _insert_in_flight(conn, imb)
    _prime_iv_token(conn)
    conn.close()

    recorder = Recorder()
    recorder.add(
        "GET",
        IV_TRACKING_URL,
        lambda _r: httpx.Response(200, json=_make_tracking_body(imb, [])),
    )

    created: list[httpx.AsyncClient] = []
    real_ctor = httpx.AsyncClient

    def _capture(**kwargs: object) -> httpx.AsyncClient:
        # Force a MockTransport; drop the production kwargs that would open
        # real sockets.
        kwargs.pop("trust_env", None)
        kwargs.pop("timeout", None)
        client = real_ctor(
            transport=httpx.MockTransport(recorder), **cast(dict[str, object], kwargs)
        )
        created.append(client)
        return client

    monkeypatch.setattr("mailwatch.poll.httpx.AsyncClient", _capture)

    result = await poll._poll_once(settings)
    assert result["polled"] == 1
    assert len(created) == 1
    assert created[0].is_closed


def test_main_returns_zero_on_success(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` is the script entrypoint used by the systemd timer.

    We stub ``asyncio.run`` so the test doesn't spin a new event loop under
    pytest-asyncio (which leaks file descriptors into the next test's setup).
    """

    async def _fake(_settings: Settings, **_kwargs: object) -> dict[str, int]:
        return {"polled": 0, "new_events": 0, "errors": 0}

    captured: list[dict[str, int]] = []

    def _fake_run(coro: object) -> dict[str, int]:
        # Drain the coroutine synchronously without a real event loop.
        import asyncio as _asyncio

        loop = _asyncio.new_event_loop()
        try:
            result: dict[str, int] = loop.run_until_complete(
                cast("object", coro)  # type: ignore[arg-type]
            )
        finally:
            loop.close()
        captured.append(result)
        return result

    monkeypatch.setattr(poll, "_poll_once", _fake)
    monkeypatch.setattr(poll.asyncio, "run", _fake_run)
    assert poll.main() == 0
    assert captured == [{"polled": 0, "new_events": 0, "errors": 0}]


def test_main_returns_one_on_unhandled_exception(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled exception propagates to a non-zero exit code, not a crash."""

    async def _boom(_settings: Settings, **_kwargs: object) -> dict[str, int]:
        raise RuntimeError("simulated pass failure")

    def _fake_run(coro: object) -> dict[str, int]:
        import asyncio as _asyncio

        loop = _asyncio.new_event_loop()
        try:
            result: dict[str, int] = loop.run_until_complete(
                cast("object", coro)  # type: ignore[arg-type]
            )
        finally:
            loop.close()
        return result

    monkeypatch.setattr(poll, "_poll_once", _boom)
    monkeypatch.setattr(poll.asyncio, "run", _fake_run)
    assert poll.main() == 1


def test_main_respects_mailwatch_poll_lookback_days_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POLL_LOOKBACK_DAYS`` must flow env → Settings → main → _poll_once.

    Regression fence: if Settings.POLL_LOOKBACK_DAYS or main()'s wiring is
    removed, the NixOS module's Environment= setting becomes silently dead.
    """
    db_path = tmp_path / "mailwatch.db"
    monkeypatch.setenv("MAILER_ID", "123456")
    monkeypatch.setenv("BSG_USERNAME", "u")
    monkeypatch.setenv("BSG_PASSWORD", "p")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_ID", "c")
    monkeypatch.setenv("USPS_NEWAPI_CUSTOMER_SECRET", "s")
    monkeypatch.setenv("SESSION_KEY", "a" * 32)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("POLL_LOOKBACK_DAYS", "1")
    get_settings.cache_clear()

    captured: list[int] = []

    async def _capture(
        _settings: Settings, *, lookback_days: int, **_kwargs: object
    ) -> dict[str, int]:
        captured.append(lookback_days)
        return {"polled": 0, "new_events": 0, "errors": 0}

    def _fake_run(coro: object) -> dict[str, int]:
        import asyncio as _asyncio

        loop = _asyncio.new_event_loop()
        try:
            result: dict[str, int] = loop.run_until_complete(
                cast("object", coro)  # type: ignore[arg-type]
            )
        finally:
            loop.close()
        return result

    monkeypatch.setattr(poll, "_poll_once", _capture)
    monkeypatch.setattr(poll.asyncio, "run", _fake_run)

    assert poll.main() == 0
    assert captured == [1]
    get_settings.cache_clear()
