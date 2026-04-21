"""Tests for :mod:`mailwatch.app` — app factory, lifespan, middleware."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient

from mailwatch.app import TOKEN_REFRESH_INTERVAL_SEC, create_app
from mailwatch.config import Settings
from mailwatch.middleware import GATED_PATH, IPAllowlistMiddleware


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Minimal ``Settings`` wired to a per-test SQLite file."""
    return Settings(
        MAILER_ID=123456,
        BSG_USERNAME="bsg-user",
        BSG_PASSWORD="bsg-pw",
        USPS_NEWAPI_CUSTOMER_ID="client-abc",
        USPS_NEWAPI_CUSTOMER_SECRET="client-secret",
        SESSION_KEY="a" * 32,
        DB_PATH=tmp_path / "mailwatch.db",
        USPS_FEED_CIDRS=["56.0.0.0/8"],
    )


@pytest.fixture
def _no_real_token_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Park the refresh loop on a harmless long sleep so tests stay fast.

    We still want the task to exist (so shutdown-cancellation is
    exercised), but we do not want it to block shutdown or issue real
    token fetches during the test.
    """
    monkeypatch.setattr("mailwatch.app.TOKEN_REFRESH_INTERVAL_SEC", 3600)


@pytest.fixture
def client(settings: Settings, _no_real_token_refresh: None) -> Iterator[TestClient]:
    """TestClient with lifespan fired (state populated, refresh task running)."""
    app = create_app(settings, session_https_only=False)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Lifespan                                                                    #
# --------------------------------------------------------------------------- #


def test_lifespan_wires_app_state(client: TestClient) -> None:
    """Every declared ``app.state.*`` attribute is present after startup."""
    app = client.app
    # Settings is set in create_app before lifespan, but we still want to
    # confirm it survived.
    assert app.state.settings is not None
    assert isinstance(app.state.db_lock, asyncio.Lock)
    assert app.state.db is not None
    assert isinstance(app.state.http, httpx.AsyncClient)
    assert app.state.rate_limiter is not None
    assert app.state.new_api is not None
    assert app.state.ivmtr is not None
    assert isinstance(app.state.refresh_task, asyncio.Task)
    assert not app.state.refresh_task.done()


def test_lifespan_shutdown_cleans_up(settings: Settings, _no_real_token_refresh: None) -> None:
    """Shutdown closes the http client + db and cancels the refresh task."""
    app = create_app(settings, session_https_only=False)
    with TestClient(app) as c:
        # Touch something to ensure startup ran.
        resp = c.get("/")
        assert resp.status_code == 200

    # After exiting the context manager the lifespan shutdown has run.
    assert app.state.refresh_task.cancelled() or app.state.refresh_task.done()
    assert app.state.http.is_closed


async def test_token_refresh_interval_constant_is_sane() -> None:
    """The default refresh cadence is below both tokens' ~1h lifetime."""
    assert 1 <= TOKEN_REFRESH_INTERVAL_SEC <= 3600


# --------------------------------------------------------------------------- #
# IPAllowlistMiddleware                                                       #
# --------------------------------------------------------------------------- #


async def test_allowlist_rejects_non_allowlisted_ip() -> None:
    """A request to ``/usps_feed`` from outside the CIDR set gets a 403."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def feed(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route(GATED_PATH, feed, methods=["POST"])])
    app.add_middleware(IPAllowlistMiddleware, cidrs=["56.0.0.0/8"])

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
            resp = await hc.post(GATED_PATH)
    assert resp.status_code == 403


async def test_allowlist_allows_cidr_match() -> None:
    """An in-CIDR source IP passes through to the handler."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def feed(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route(GATED_PATH, feed, methods=["POST"])])
    app.add_middleware(IPAllowlistMiddleware, cidrs=["56.0.0.0/8"])

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, client=("56.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
            resp = await hc.post(GATED_PATH)
    assert resp.status_code == 200
    assert resp.text == "ok"


async def test_allowlist_bypasses_non_gated_paths() -> None:
    """Paths other than ``/usps_feed`` are never inspected."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("hi")

    app = Starlette(routes=[Route("/other", ok, methods=["GET"])])
    app.add_middleware(IPAllowlistMiddleware, cidrs=["56.0.0.0/8"])

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
            resp = await hc.get("/other")
    assert resp.status_code == 200


async def test_allowlist_rejects_unparseable_ip() -> None:
    """A malformed source IP (e.g. garbage from a bad proxy config) is rejected."""
    # We drive the middleware by hand with a crafted ASGI scope so we can
    # set ``client`` to something that survives Starlette's Request
    # construction but trips ``ip_address()``.
    from starlette.middleware.base import BaseHTTPMiddleware

    assert issubclass(IPAllowlistMiddleware, BaseHTTPMiddleware)

    received: list[object] = []

    async def app_under_test(scope, receive, send):  # type: ignore[no-untyped-def]
        received.append(("app", scope["path"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = IPAllowlistMiddleware(app_under_test, cidrs=["56.0.0.0/8"])

    responses: list[dict[str, object]] = []

    async def send(message):  # type: ignore[no-untyped-def]
        responses.append(message)

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": GATED_PATH,
        "raw_path": GATED_PATH.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("not-an-ip", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
    }
    await middleware(scope, receive, send)
    starts = [m for m in responses if m.get("type") == "http.response.start"]
    assert starts and starts[0]["status"] == 403
    # App was never called because the middleware short-circuited.
    assert ("app", GATED_PATH) not in received
