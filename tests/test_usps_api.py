"""Tests for mailwatch.usps_api — both USPS clients.

Uses :class:`httpx.MockTransport` (in httpx core, no extra deps) to intercept
requests. Each test gets a tmp_path-backed SQLite DB so app_state + cache
writes are isolated.

The tenacity retry decorator sleeps via ``asyncio.sleep`` between attempts;
tests monkeypatch ``asyncio.sleep`` to a no-op so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from mailwatch import db
from mailwatch.config import Settings
from mailwatch.models import AddressRequest
from mailwatch.rate_limit import RateLimiter
from mailwatch.usps_api import (
    IV_AUTH_URL,
    IV_TOKEN_URL,
    IV_TRACKING_URL,
    NEW_API_ADDRESS_URL,
    NEW_API_TOKEN_URL,
    IVMTRClient,
    NewApiClient,
)

# --- shared fixtures -------------------------------------------------------- #


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(tmp_path / "mailwatch.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def config() -> Settings:
    """A Settings instance wired to stable test values (no env coupling)."""
    return Settings(
        MAILER_ID=123456,
        BSG_USERNAME="bsg-user",
        BSG_PASSWORD="bsg-pw",
        USPS_NEWAPI_CUSTOMER_ID="client-abc",
        USPS_NEWAPI_CUSTOMER_SECRET="client-secret",
        SESSION_KEY="a" * 32,
    )


@pytest.fixture
def address_request() -> AddressRequest:
    return AddressRequest(
        streetAddress="1600 Pennsylvania Ave NW",
        city="Washington",
        state="DC",
        ZIPCode="20500",
    )


@pytest.fixture
def address_response_body() -> dict[str, object]:
    return {
        "firm": None,
        "address": {
            "streetAddress": "1600 PENNSYLVANIA AVE NW",
            "city": "WASHINGTON",
            "state": "DC",
            "ZIPCode": "20500",
            "ZIPPlus4": "0005",
        },
        "additionalInfo": {
            "deliveryPoint": "99",
            "DPVConfirmation": "Y",
        },
    }


@pytest.fixture
def tracking_response_body() -> dict[str, object]:
    return {
        "data": {
            "imb": "9" * 31,
            "scans": [
                {
                    "imb": "9" * 31,
                    "scanDatetime": "2026-04-21T09:00:00Z",
                    "scanEventCode": "SD",
                    "scanFacilityCity": "WASHINGTON",
                    "scanFacilityState": "DC",
                    "scanFacilityZip": "20018",
                }
            ],
        }
    }


@pytest.fixture
def new_api_token_body() -> dict[str, object]:
    return {
        "access_token": "new-api-token-xyz",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "addresses",
    }


@pytest.fixture
def iv_token_body() -> dict[str, object]:
    return {
        "access_token": "iv-access-abc",
        "refresh_token": "iv-refresh-abc",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse tenacity's exp-backoff sleeps so tests run in milliseconds."""

    async def _zero(_seconds: float) -> None:
        return None

    monkeypatch.setattr("tenacity.nap.sleep", _zero)


# --- helpers --------------------------------------------------------------- #


class CallRecorder:
    """Records each intercepted request; pairs with ``httpx.MockTransport``.

    Handlers register responses keyed on (method, url prefix). ``calls``
    exposes every intercepted request so tests can assert "no network hit"
    or "exactly N retries to URL X".
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        # list of (method, url_predicate, handler_fn)
        self._handlers: list[
            tuple[str, Callable[[str], bool], Callable[[httpx.Request], httpx.Response]]
        ] = []

    def add(
        self,
        method: str,
        url_or_pred: str | Callable[[str], bool],
        responses: list[httpx.Response] | Callable[[httpx.Request], httpx.Response],
    ) -> None:
        """Register responses for a method + URL (prefix match or predicate).

        ``responses`` may be a list (consumed one per call, last repeats) or
        a handler function that inspects the request and returns a Response.
        """
        if callable(url_or_pred):
            pred = url_or_pred
        else:

            def _eq(u: str, expected: str = url_or_pred) -> bool:
                return u == expected or u.startswith((expected + "?", expected + "/"))

            pred = _eq

        if callable(responses):
            handler = responses
        else:
            queue: list[httpx.Response] = list(responses)

            def _pop(_req: httpx.Request, q: list[httpx.Response] = queue) -> httpx.Response:
                if len(q) == 1:
                    return q[0]
                return q.pop(0)

            handler = _pop

        self._handlers.append((method, pred, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, pred, handler in self._handlers:
            if request.method == method and pred(url):
                return handler(request)
        raise AssertionError(f"Unregistered {request.method} {url}")


def _make_client(recorder: CallRecorder) -> httpx.AsyncClient:
    transport = httpx.MockTransport(recorder)
    return httpx.AsyncClient(transport=transport)


# --------------------------------------------------------------------------- #
# NewApiClient: token lifecycle                                               #
# --------------------------------------------------------------------------- #


async def test_new_api_token_fetch_persists_to_db(
    conn: sqlite3.Connection,
    config: Settings,
    new_api_token_body: dict[str, object],
) -> None:
    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        token = await client.get_access_token()

    assert token == "new-api-token-xyz"
    assert db.get_state(conn, "new_api_access_token") == b"new-api-token-xyz"
    expiry = float((db.get_state(conn, "new_api_token_expiry") or b"0").decode())
    assert expiry > time.time()
    assert len(recorder.calls) == 1


async def test_new_api_token_cache_hit_skips_network(
    conn: sqlite3.Connection, config: Settings
) -> None:
    db.set_state(conn, "new_api_access_token", "cached-token")
    db.set_state(conn, "new_api_token_expiry", f"{time.time() + 600:.3f}")

    recorder = CallRecorder()  # no handlers registered — any call would raise

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        token = await client.get_access_token()

    assert token == "cached-token"
    assert recorder.calls == []


async def test_new_api_token_refresh_when_expired(
    conn: sqlite3.Connection,
    config: Settings,
    new_api_token_body: dict[str, object],
) -> None:
    db.set_state(conn, "new_api_access_token", "stale")
    db.set_state(conn, "new_api_token_expiry", "0")

    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        token = await client.get_access_token()

    assert token == "new-api-token-xyz"
    assert len(recorder.calls) == 1


# --------------------------------------------------------------------------- #
# NewApiClient: validate_address                                              #
# --------------------------------------------------------------------------- #


async def test_validate_address_success(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    new_api_token_body: dict[str, object],
    address_response_body: dict[str, object],
) -> None:
    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])
    recorder.add("GET", NEW_API_ADDRESS_URL, [httpx.Response(200, json=address_response_body)])

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        result = await client.validate_address(address_request)

    assert result.address.ZIPCode == "20500"
    assert result.address.ZIPPlus4 == "0005"
    assert result.full_zip == "20500-0005"
    # Cache written
    cache_key = db.hash_address_dict(address_request.model_dump(exclude_none=True))
    assert db.cache_get(conn, cache_key) is not None


async def test_validate_address_cache_hit_skips_network(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    address_response_body: dict[str, object],
) -> None:
    cache_key = db.hash_address_dict(address_request.model_dump(exclude_none=True))
    db.cache_put(conn, cache_key, json.dumps(address_response_body).encode())

    recorder = CallRecorder()  # no handlers — any call would assert

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        result = await client.validate_address(address_request)

    assert result.address.ZIPCode == "20500"
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# NewApiClient: retry + 401 token-refresh                                     #
# --------------------------------------------------------------------------- #


async def test_new_api_401_invalidates_token_and_retries_once(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    new_api_token_body: dict[str, object],
    address_response_body: dict[str, object],
) -> None:
    # Pre-populate a valid-looking token so the first call skips the token POST.
    db.set_state(conn, "new_api_access_token", "initially-valid")
    db.set_state(conn, "new_api_token_expiry", f"{time.time() + 600:.3f}")

    recorder = CallRecorder()
    # First address call 401s, second succeeds.
    recorder.add(
        "GET",
        NEW_API_ADDRESS_URL,
        [
            httpx.Response(401, json={"error": "unauthorized"}),
            httpx.Response(200, json=address_response_body),
        ],
    )
    # The invalidation triggers a fresh token POST.
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        result = await client.validate_address(address_request)

    assert result.address.ZIPCode == "20500"
    # Expect: 1 GET (401), 1 POST /token, 1 GET (200)
    methods = [(r.method, str(r.url).split("?")[0]) for r in recorder.calls]
    assert methods.count(("GET", NEW_API_ADDRESS_URL)) == 2
    assert methods.count(("POST", NEW_API_TOKEN_URL)) == 1


async def test_new_api_429_retries_with_backoff(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    new_api_token_body: dict[str, object],
    address_response_body: dict[str, object],
) -> None:
    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])
    recorder.add(
        "GET",
        NEW_API_ADDRESS_URL,
        [
            httpx.Response(429, json={"error": "rate_limited"}),
            httpx.Response(429, json={"error": "rate_limited"}),
            httpx.Response(200, json=address_response_body),
        ],
    )

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        result = await client.validate_address(address_request)

    assert result.address.ZIPCode == "20500"
    address_calls = [r for r in recorder.calls if str(r.url).startswith(NEW_API_ADDRESS_URL)]
    assert len(address_calls) == 3  # two 429 + one 200


async def test_new_api_500_retries_then_gives_up(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    new_api_token_body: dict[str, object],
) -> None:
    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])
    # 503 is in RETRYABLE_STATUS_CODES; exhaust stop_after_attempt(3).
    recorder.add(
        "GET",
        NEW_API_ADDRESS_URL,
        [
            httpx.Response(503, json={"error": "upstream"}),
            httpx.Response(503, json={"error": "upstream"}),
            httpx.Response(503, json={"error": "upstream"}),
        ],
    )

    async with _make_client(recorder) as http:
        client = NewApiClient(config, conn, RateLimiter(max_per_window=10, window_seconds=1), http)
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.validate_address(address_request)
        assert excinfo.value.response.status_code == 503

    address_calls = [r for r in recorder.calls if str(r.url).startswith(NEW_API_ADDRESS_URL)]
    assert len(address_calls) == 3


# --------------------------------------------------------------------------- #
# Rate-limiter engagement                                                     #
# --------------------------------------------------------------------------- #


async def test_rate_limiter_engaged_on_concurrent_requests(
    conn: sqlite3.Connection,
    config: Settings,
    address_request: AddressRequest,
    new_api_token_body: dict[str, object],
    address_response_body: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-enable a real (small) asyncio sleep for the limiter — our autouse
    # fixture replaces tenacity.nap.sleep, not asyncio.sleep.
    recorder = CallRecorder()
    recorder.add("POST", NEW_API_TOKEN_URL, [httpx.Response(200, json=new_api_token_body)])
    recorder.add(
        "GET",
        NEW_API_ADDRESS_URL,
        lambda _req: httpx.Response(200, json=address_response_body),
    )

    # 2 calls per 0.3s. With 3 concurrent calls, the third must be delayed.
    limiter = RateLimiter(max_per_window=2, window_seconds=0.3)

    async with _make_client(recorder) as http:
        # Warm the token cache first so token POST doesn't eat a slot.
        db.set_state(conn, "new_api_access_token", "warm")
        db.set_state(conn, "new_api_token_expiry", f"{time.time() + 600:.3f}")
        # Also warm the cache-key so we DO hit the network path but with
        # a different address per call by mutating ZIPCode.
        client = NewApiClient(config, conn, limiter, http)
        reqs = [
            AddressRequest(
                streetAddress="1 Main St",
                city="Springfield",
                state="IL",
                ZIPCode=f"6270{i}",
            )
            for i in range(3)
        ]
        start = asyncio.get_event_loop().time()
        results = await asyncio.gather(*(client.validate_address(r) for r in reqs))
        elapsed = asyncio.get_event_loop().time() - start

    assert len(results) == 3
    # Third request had to wait for a slot, so elapsed should be at least
    # ~window_seconds minus scheduling slack.
    assert elapsed >= 0.2, f"rate limiter did not delay: {elapsed=}"


# --------------------------------------------------------------------------- #
# IVMTRClient                                                                 #
# --------------------------------------------------------------------------- #


async def test_iv_auth_with_bsg_creds(
    conn: sqlite3.Connection,
    config: Settings,
    iv_token_body: dict[str, object],
) -> None:
    recorder = CallRecorder()
    recorder.add("POST", IV_AUTH_URL, [httpx.Response(200, json=iv_token_body)])

    async with _make_client(recorder) as http:
        client = IVMTRClient(config, conn, http)
        token = await client.get_access_token()

    assert token == "iv-access-abc"
    assert db.get_state(conn, "iv_access_token") == b"iv-access-abc"
    assert db.get_state(conn, "iv_refresh_token") == b"iv-refresh-abc"
    # Request body carried the BSG creds
    assert len(recorder.calls) == 1
    posted = json.loads(recorder.calls[0].content)
    assert posted["username"] == "bsg-user"
    assert posted["password"] == "bsg-pw"
    assert posted["grant_type"] == "authorization"


async def test_iv_uses_refresh_token_when_available(
    conn: sqlite3.Connection,
    config: Settings,
    iv_token_body: dict[str, object],
) -> None:
    # Expired access, valid refresh token.
    db.set_state(conn, "iv_access_token", "expired")
    db.set_state(conn, "iv_refresh_token", "my-refresh-token")
    db.set_state(conn, "iv_token_expiry", "0")

    recorder = CallRecorder()
    # Only /oauth/token should be called — NOT /oauth/authenticate.
    recorder.add("POST", IV_TOKEN_URL, [httpx.Response(200, json=iv_token_body)])

    async with _make_client(recorder) as http:
        client = IVMTRClient(config, conn, http)
        token = await client.get_access_token()

    assert token == "iv-access-abc"
    assert len(recorder.calls) == 1
    assert str(recorder.calls[0].url) == IV_TOKEN_URL
    posted = json.loads(recorder.calls[0].content)
    assert posted["refresh_token"] == "my-refresh-token"


async def test_iv_get_tracking_parses_response(
    conn: sqlite3.Connection,
    config: Settings,
    iv_token_body: dict[str, object],
    tracking_response_body: dict[str, object],
) -> None:
    imb = "9" * 31
    recorder = CallRecorder()
    recorder.add("POST", IV_AUTH_URL, [httpx.Response(200, json=iv_token_body)])
    recorder.add(
        "GET",
        IV_TRACKING_URL,
        [httpx.Response(200, json=tracking_response_body)],
    )

    async with _make_client(recorder) as http:
        client = IVMTRClient(config, conn, http)
        result = await client.get_tracking(imb)

    assert result.data is not None
    assert result.data.imb == imb
    assert len(result.data.scans) == 1
    assert result.data.scans[0].scanEventCode == "SD"
