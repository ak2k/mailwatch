"""Async HTTP clients for USPS developer APIs.

Two distinct clients with independent OAuth lifecycles:

* :class:`NewApiClient` — modern ``apis.usps.com`` OAuth 2 client_credentials
  grant, address standardization endpoint. Shares the process-wide
  :class:`~mailwatch.rate_limit.RateLimiter` for outbound calls (USPS quotas
  this host aggressively).
* :class:`IVMTRClient` — legacy Business Services Gateway (BSG) OAuth flow at
  ``services.usps.com``, used only to obtain tracking data from
  ``iv.usps.com``. Separate rate bucket — does not share the NewApi limiter.

Design notes
------------
* Neither client owns its :class:`httpx.AsyncClient`. A single client is
  created by the FastAPI lifespan context manager (Wave 3) and injected; this
  keeps connection pooling + HTTP/2 benefits, and makes shutdown deterministic.
* Token state lives in SQLite (``app_state`` K/V), so a process restart
  picks up the cached token instead of re-authenticating. DB access is sync;
  every read/write is wrapped in :func:`asyncio.to_thread` to avoid blocking
  the event loop.
* Transient 5xx/429 errors are retried with jittered exponential backoff via
  :mod:`tenacity`. 401 is handled separately — it indicates token expiry and
  needs a one-shot token refresh + retry (not backoff).
* Address-validation responses are cached in the ``address_cache`` table
  keyed by the canonical address hash (see :func:`db.hash_address_dict`).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from mailwatch import db
from mailwatch.config import Settings
from mailwatch.models import (
    AddressRequest,
    IVMTRTokenResponse,
    NewApiTokenResponse,
    StandardizedAddressResponse,
    TrackingResponse,
)
from mailwatch.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

# --- USPS endpoints (the only hardcoded URLs in this module) ---------------- #

NEW_API_BASE = "https://apis.usps.com"
NEW_API_TOKEN_URL = f"{NEW_API_BASE}/oauth2/v3/token"
NEW_API_ADDRESS_URL = f"{NEW_API_BASE}/addresses/v3/address"

SERVICES_BASE = "https://services.usps.com"
IV_AUTH_URL = f"{SERVICES_BASE}/oauth/authenticate"
IV_TOKEN_URL = f"{SERVICES_BASE}/oauth/token"

IV_API_BASE = "https://iv.usps.com"
IV_TRACKING_URL = f"{IV_API_BASE}/ivws_api/informedvisapi/api/mt/get/piece/imb"

# BSG's fixed client_id for the IV-MTR OAuth flow — public (it's in USPS's
# sample code); the secret is the operator's BSG password.
IV_CLIENT_ID = "687b8a36-db61-42f7-83f7-11c79bf7785e"
IV_SCOPE = "user.info.ereg,iv1.apis"

# HTTP status codes that warrant a retry with backoff. 401 is handled
# separately (token invalidation + one-shot retry), not via this set.
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


def _is_retryable_status_error(exc: BaseException) -> bool:
    """Tenacity predicate: retry only on transient USPS-side failures."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


T = TypeVar("T")


async def _run_db(lock: asyncio.Lock, fn: Callable[..., T], *args: object) -> T:
    """Run a sync DB call on a worker thread under a per-client lock.

    stdlib ``sqlite3`` Connections are not safe for concurrent use across
    threads even with ``check_same_thread=False`` — the flag suppresses the
    guard but doesn't make the API re-entrant. Serialising every call to
    ``asyncio.to_thread`` with one ``asyncio.Lock`` per client avoids the
    ``InterfaceError`` that otherwise fires when two coroutines hit the same
    connection simultaneously.
    """
    async with lock:
        return await asyncio.to_thread(fn, *args)


# --------------------------------------------------------------------------- #
# apis.usps.com                                                               #
# --------------------------------------------------------------------------- #


class NewApiClient:
    """Client for the modern ``apis.usps.com`` endpoints.

    Today this covers OAuth token acquisition + address standardization.
    Additional endpoints (tracking-by-tracking-number, label purchase, etc.)
    would slot in as additional ``async def`` methods reusing the same token
    + rate-limit + retry scaffolding.
    """

    def __init__(
        self,
        config: Settings,
        db_conn: sqlite3.Connection,
        rate_limiter: RateLimiter,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._db = db_conn
        self._limiter = rate_limiter
        self._http = http_client
        self._db_lock = asyncio.Lock()

    # --- token lifecycle --------------------------------------------------- #

    async def _cached_token(self) -> tuple[str | None, float]:
        """Return (token, expiry_epoch) from SQLite, or (None, 0.0) if absent."""
        token_bytes = await _run_db(self._db_lock, db.get_state, self._db, "new_api_access_token")
        expiry_bytes = await _run_db(self._db_lock, db.get_state, self._db, "new_api_token_expiry")
        if token_bytes is None or expiry_bytes is None:
            return None, 0.0
        try:
            expiry = float(expiry_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, 0.0
        return token_bytes.decode("utf-8"), expiry

    async def _store_token(self, parsed: NewApiTokenResponse) -> None:
        """Persist a freshly issued token + its absolute expiry time."""
        # Refresh pre-emptively at half of the advertised lifetime so a
        # request that starts near the boundary doesn't race the clock.
        lifetime = max(1, int(parsed.expires_in))
        expiry = time.time() + (lifetime / 2.0)
        await _run_db(
            self._db_lock, db.set_state, self._db, "new_api_access_token", parsed.access_token
        )
        await _run_db(
            self._db_lock, db.set_state, self._db, "new_api_token_expiry", f"{expiry:.3f}"
        )

    async def _invalidate_token(self) -> None:
        """Force the next :meth:`get_access_token` to re-fetch (used on 401)."""
        await _run_db(self._db_lock, db.set_state, self._db, "new_api_token_expiry", "0")

    async def get_access_token(self) -> str:
        """Return a valid access token, fetching a new one if expired.

        The token is cached in ``app_state`` so a process restart doesn't
        invalidate it.
        """
        token, expiry = await self._cached_token()
        if token is not None and expiry > time.time():
            return token
        return await self._fetch_new_token()

    async def _fetch_new_token(self) -> str:
        """POST to ``/oauth2/v3/token`` and persist the new token."""
        payload = {
            "client_id": self._config.USPS_NEWAPI_CUSTOMER_ID,
            "client_secret": self._config.USPS_NEWAPI_CUSTOMER_SECRET.get_secret_value(),
            "grant_type": "client_credentials",
        }
        async with self._limiter:
            resp = await self._http.post(NEW_API_TOKEN_URL, json=payload)
        resp.raise_for_status()
        parsed = NewApiTokenResponse.model_validate(resp.json())
        await self._store_token(parsed)
        return parsed.access_token

    # --- address standardization ------------------------------------------ #

    async def validate_address(self, req: AddressRequest) -> StandardizedAddressResponse:
        """Standardize an address. Cached per canonical request hash."""
        request_dict = req.model_dump(exclude_none=True)
        cache_key = db.hash_address_dict(request_dict)

        cached = await _run_db(self._db_lock, db.cache_get, self._db, cache_key)
        if cached is not None:
            return StandardizedAddressResponse.model_validate_json(cached)

        resp_json = await self._fetch_address(request_dict)
        await _run_db(self._db_lock, db.cache_put, self._db, cache_key, resp_json.encode("utf-8"))
        return StandardizedAddressResponse.model_validate_json(resp_json)

    async def _fetch_address(self, params: dict[str, str]) -> str:
        """Fetch + retry. Returns the raw JSON body as a string.

        The retry strategy splits on failure mode:

        * ``tenacity`` handles 429 / 5xx transients with jittered exp backoff.
        * 401 is treated as token expiry — invalidate, refresh, retry once.
          Backoff wouldn't help (the token isn't going to un-expire), so we
          don't use tenacity for this case.
        """

        @retry(
            retry=retry_if_exception(_is_retryable_status_error),
            wait=wait_exponential_jitter(initial=1, max=30),
            stop=stop_after_attempt(3),
            reraise=True,
        )
        async def _do_request() -> httpx.Response:
            token = await self.get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            async with self._limiter:
                resp = await self._http.get(NEW_API_ADDRESS_URL, headers=headers, params=params)
            # Only raise for the statuses tenacity should retry — 401 has
            # its own handling below and must not be caught here.
            if resp.status_code in RETRYABLE_STATUS_CODES:
                resp.raise_for_status()
            return resp

        resp = await _do_request()
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            # Token likely expired between the cache check and the call.
            # Invalidate and retry exactly once with a fresh token.
            logger.info("USPS NewApi returned 401; invalidating token and retrying once")
            await self._invalidate_token()
            resp = await _do_request()

        resp.raise_for_status()
        return resp.text


# --------------------------------------------------------------------------- #
# services.usps.com + iv.usps.com                                             #
# --------------------------------------------------------------------------- #


class IVMTRClient:
    """Client for IV-MTR tracking pulls via the BSG OAuth flow.

    IV-MTR uses a distinct auth endpoint (``services.usps.com``) with a
    refresh_token grant, and the tracking API lives on yet another host
    (``iv.usps.com``). Quotas are also separate from apis.usps.com, so this
    client does *not* share the NewApi rate limiter.
    """

    def __init__(
        self,
        config: Settings,
        db_conn: sqlite3.Connection,
        http_client: httpx.AsyncClient,
        iv_limiter: RateLimiter | None = None,
    ) -> None:
        self._config = config
        self._db = db_conn
        self._http = http_client
        self._limiter = iv_limiter
        self._db_lock = asyncio.Lock()

    # --- token lifecycle --------------------------------------------------- #

    async def _cached_tokens(self) -> tuple[str | None, str | None, float]:
        """Return (access, refresh, expiry_epoch) — any may be None/0."""
        access = await _run_db(self._db_lock, db.get_state, self._db, "iv_access_token")
        refresh = await _run_db(self._db_lock, db.get_state, self._db, "iv_refresh_token")
        expiry_bytes = await _run_db(self._db_lock, db.get_state, self._db, "iv_token_expiry")
        try:
            expiry = float(expiry_bytes.decode("utf-8")) if expiry_bytes else 0.0
        except (UnicodeDecodeError, ValueError):
            expiry = 0.0
        return (
            access.decode("utf-8") if access else None,
            refresh.decode("utf-8") if refresh else None,
            expiry,
        )

    async def _store_tokens(self, parsed: IVMTRTokenResponse) -> None:
        """Persist a freshly issued IV-MTR token trio."""
        lifetime = max(1, int(parsed.expires_in))
        expiry = time.time() + (lifetime / 2.0)
        await _run_db(self._db_lock, db.set_state, self._db, "iv_access_token", parsed.access_token)
        await _run_db(
            self._db_lock, db.set_state, self._db, "iv_refresh_token", parsed.refresh_token
        )
        await _run_db(self._db_lock, db.set_state, self._db, "iv_token_expiry", f"{expiry:.3f}")

    async def _maybe_rate_limit(self) -> None:
        """Acquire the IV rate limiter if one was provided."""
        if self._limiter is not None:
            await self._limiter.acquire()

    async def get_access_token(self) -> str:
        """Return a valid IV-MTR access token, refreshing as needed.

        Selection order:

        1. Cached access token that hasn't hit pre-emptive expiry — use it.
        2. Cached refresh token — call ``/oauth/token`` to trade for a new access.
        3. Otherwise — full BSG auth at ``/oauth/authenticate`` with user+pass.
        """
        access, refresh, expiry = await self._cached_tokens()
        if access is not None and expiry > time.time():
            return access

        if refresh is not None:
            try:
                return await self._refresh_access_token(refresh)
            except httpx.HTTPStatusError as exc:
                # Refresh token likely revoked/expired; fall back to full auth.
                logger.info("IV-MTR refresh failed (%s); falling back to full auth", exc)

        return await self._authenticate()

    async def _authenticate(self) -> str:
        """Full BSG login with username + password."""
        payload = {
            "username": self._config.BSG_USERNAME,
            "password": self._config.BSG_PASSWORD.get_secret_value(),
            "grant_type": "authorization",
            "response_type": "token",
            "scope": IV_SCOPE,
            "client_id": IV_CLIENT_ID,
        }
        await self._maybe_rate_limit()
        resp = await self._http.post(IV_AUTH_URL, json=payload)
        resp.raise_for_status()
        parsed = IVMTRTokenResponse.model_validate(resp.json())
        await self._store_tokens(parsed)
        return parsed.access_token

    async def _refresh_access_token(self, refresh_token: str) -> str:
        """Trade a refresh_token for a new access_token."""
        payload = {
            "refresh_token": refresh_token,
            "grant_type": "authorization",
            "response_type": "token",
            "scope": IV_SCOPE,
        }
        await self._maybe_rate_limit()
        resp = await self._http.post(IV_TOKEN_URL, json=payload)
        resp.raise_for_status()
        parsed = IVMTRTokenResponse.model_validate(resp.json())
        await self._store_tokens(parsed)
        return parsed.access_token

    # --- tracking pulls ---------------------------------------------------- #

    async def get_tracking(self, imb: str) -> TrackingResponse:
        """Fetch tracking scans for a given IMb.

        The IV-MTR endpoint is less flaky than apis.usps.com (it's a pull
        API against a reporting warehouse) so we use the same retry strategy
        but trust the 401 path less aggressively — a 401 here typically
        means the refresh token is stale and we need to re-auth. One retry.
        """

        @retry(
            retry=retry_if_exception(_is_retryable_status_error),
            wait=wait_exponential_jitter(initial=1, max=30),
            stop=stop_after_attempt(3),
            reraise=True,
        )
        async def _do_request() -> httpx.Response:
            token = await self.get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            await self._maybe_rate_limit()
            resp = await self._http.get(f"{IV_TRACKING_URL}/{imb}", headers=headers)
            if resp.status_code in RETRYABLE_STATUS_CODES:
                resp.raise_for_status()
            return resp

        resp = await _do_request()
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            logger.info("IV-MTR returned 401; invalidating tokens and retrying once")
            await _run_db(self._db_lock, db.set_state, self._db, "iv_token_expiry", "0")
            await _run_db(self._db_lock, db.set_state, self._db, "iv_refresh_token", "")
            resp = await _do_request()

        resp.raise_for_status()
        return TrackingResponse.model_validate_json(resp.text)
