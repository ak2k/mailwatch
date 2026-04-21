"""Starlette middleware helpers for mailwatch.

Only one piece of middleware lives here today — :class:`IPAllowlistMiddleware`,
which gates the IV-MTR push-feed webhook path (``/usps_feed``) to a
configurable list of CIDR blocks. USPS publishes the push-feed source
ranges; deliveries from anywhere else should be rejected outright.

All other request paths pass through unchanged. The middleware therefore
never rewrites or rejects ordinary user traffic — it exists purely to
constrain a single, unauthenticated ingress point.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from ipaddress import ip_address, ip_network
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# The single gated path. Kept as a module-level constant so tests and
# operators can reference it without re-typing the literal.
GATED_PATH: Final = "/usps_feed"


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Reject requests to ``/usps_feed`` from non-allowlisted source IPs.

    The check only fires for the gated path; every other path is a no-op
    passthrough. ``request.client.host`` is read *after* any upstream
    :class:`~uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware` has
    rewritten it based on ``X-Forwarded-For``, so behind a trusted reverse
    proxy the allowlist checks the true source address.
    """

    def __init__(self, app: ASGIApp, cidrs: list[str]) -> None:
        super().__init__(app)
        self._nets = [ip_network(c) for c in cidrs]

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == GATED_PATH:
            client_host = request.client.host if request.client else None
            if not client_host:
                return Response("forbidden", status_code=403)
            try:
                ip = ip_address(client_host)
            except ValueError:
                return Response("forbidden", status_code=403)
            if not any(ip in net for net in self._nets):
                return Response("forbidden", status_code=403)
        return await call_next(request)
