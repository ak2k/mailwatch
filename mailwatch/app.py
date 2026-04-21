"""FastAPI application factory + lifespan for mailwatch.

Lifespan owns every piece of shared state:

* SQLite connection (WAL-mode, autocommit) + an :class:`asyncio.Lock`
  serialising every ``to_thread(db.*)`` call — stdlib ``sqlite3`` is not
  re-entrant across threads even with ``check_same_thread=False``.
* :class:`httpx.AsyncClient` — one pool, shared by both USPS clients so
  connection reuse + HTTP/2 actually happen.
* :class:`~mailwatch.rate_limit.RateLimiter` sized to the configured
  ``RATE_LIMIT_PER_HOUR``, shared by the NewApi client.
* :class:`~mailwatch.usps_api.NewApiClient` and
  :class:`~mailwatch.usps_api.IVMTRClient`.
* A background :class:`asyncio.Task` that refreshes both OAuth tokens
  every ``TOKEN_REFRESH_INTERVAL_SEC`` so no request pays a round-trip
  to USPS just to mint a token.

Middleware is added in a specific order — see the comment on
:func:`create_app` for the rationale.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mailwatch import db
from mailwatch.config import Settings, get_settings
from mailwatch.middleware import IPAllowlistMiddleware
from mailwatch.rate_limit import RateLimiter
from mailwatch.routes import router
from mailwatch.usps_api import IVMTRClient, NewApiClient

logger = logging.getLogger(__name__)

# Pre-emptive token refresh cadence. Tokens advertise long lifetimes (~1h)
# but the clients already halve that internally, so 5-minute refreshes give
# the "never expire mid-request" property without hammering USPS.
TOKEN_REFRESH_INTERVAL_SEC = 300

_STATIC_DIR = Path(__file__).parent / "static"


async def _token_refresh_loop(app: FastAPI) -> None:
    """Periodically refresh both USPS OAuth tokens.

    Runs until cancelled on app shutdown. Both clients cache their tokens
    in SQLite; calling ``get_access_token`` is a cache hit when a token
    is still valid and a network fetch when it has lapsed.
    """
    new_api: NewApiClient = app.state.new_api
    ivmtr: IVMTRClient = app.state.ivmtr
    while True:
        try:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        for name, coro in (
            ("new_api", new_api.get_access_token()),
            ("ivmtr", ivmtr.get_access_token()),
        ):
            try:
                await coro
            except Exception as exc:
                logger.warning("token refresh (%s) failed: %s", name, exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot shared state, then tear it down cleanly on shutdown."""
    settings: Settings = app.state.settings  # set in create_app
    app.state.db_lock = asyncio.Lock()

    conn = db.connect(settings.DB_PATH)
    db.init_db(conn)
    app.state.db = conn

    # ``trust_env=False`` — we don't want to inadvertently route USPS traffic
    # through a developer's HTTP(S)_PROXY / SOCKS env. All proxy policy lives
    # in the deployment environment (nginx upstream / systemd exec env), not
    # in whatever shell the server happened to inherit.
    app.state.http = httpx.AsyncClient(timeout=30.0, trust_env=False)
    app.state.rate_limiter = RateLimiter(
        max_per_window=settings.RATE_LIMIT_PER_HOUR,
        window_seconds=3600.0,
    )
    app.state.new_api = NewApiClient(settings, conn, app.state.rate_limiter, app.state.http)
    app.state.ivmtr = IVMTRClient(settings, conn, app.state.http)

    app.state.refresh_task = asyncio.create_task(_token_refresh_loop(app))

    try:
        yield
    finally:
        app.state.refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.refresh_task
        await app.state.http.aclose()
        app.state.db.close()


def create_app(
    settings: Settings | None = None,
    *,
    session_https_only: bool = True,
) -> FastAPI:
    """Build a fresh :class:`FastAPI` app bound to ``settings``.

    Middleware execution order at request time is Proxy -> IPAllowlist ->
    Session -> route handler. Starlette's middleware stack is LIFO with
    respect to ``add_middleware`` calls: the middleware added *last* runs
    *first* on the incoming request. So we add in the reverse of the
    intended execution order:

    1. ``SessionMiddleware`` (runs last, innermost) — deserialises the
       cookie into ``request.session``.
    2. ``IPAllowlistMiddleware`` (runs second) — rejects ``/usps_feed``
       deliveries from non-USPS IPs. Reads ``request.client.host`` which
       has been rewritten by ``ProxyHeadersMiddleware`` above it.
    3. ``ProxyHeadersMiddleware`` (runs first, outermost) — rewrites
       ``request.client.host`` from ``X-Forwarded-For`` when behind a
       trusted reverse proxy (nginx on localhost by default).
    """
    _settings = settings if settings is not None else get_settings()
    app = FastAPI(lifespan=lifespan)
    app.state.settings = _settings

    # Static assets (single file today: style.css).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Add middleware in reverse execution order (Starlette stacks LIFO).
    app.add_middleware(
        SessionMiddleware,
        secret_key=_settings.SESSION_KEY.get_secret_value(),
        https_only=session_https_only,
        same_site="lax",
    )
    app.add_middleware(IPAllowlistMiddleware, cidrs=_settings.USPS_FEED_CIDRS)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="127.0.0.1")

    app.include_router(router)
    return app
