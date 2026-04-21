"""Async rolling-window rate limiter.

Caps outbound calls (e.g. to ``apis.usps.com``) at ``max_per_window`` calls per
``window_seconds`` using a sliding window. Shared across coroutines inside a
single event loop; not thread-safe (per-event-loop state only).

Usage::

    limiter = RateLimiter(max_per_window=50, window_seconds=3600)


    async def call_usps(url: str) -> httpx.Response:
        async with limiter:
            return await client.get(url)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from types import TracebackType


class RateLimiter:
    """Rolling-window rate limiter.

    Permits at most ``max_per_window`` calls in any trailing
    ``window_seconds`` interval. Uses ``time.monotonic()`` so wall-clock
    jumps do not perturb the window.

    Fairness: waiters acquire the internal :class:`asyncio.Lock` FIFO, so
    callers that started waiting earlier get their slot first. The lock is
    released while sleeping so a waiter blocking on a full window does not
    starve others that are still inside the lock inspecting the deque.
    """

    def __init__(self, max_per_window: int, window_seconds: float = 3600.0) -> None:
        if max_per_window < 1:
            raise ValueError("max_per_window must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_per_window
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        """Drop timestamps that have fallen out of the trailing window."""
        cutoff = now - self._window
        calls = self._calls
        while calls and calls[0] <= cutoff:
            calls.popleft()

    async def acquire(self) -> None:
        """Block until a slot is available, then record this call's timestamp."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return
                # Full: compute how long until the oldest call falls out.
                oldest = self._calls[0]
                wait = self._window - (now - oldest)
            # Sleep outside the lock so we don't starve other waiters.
            await asyncio.sleep(max(0.0, wait))

    async def __aenter__(self) -> RateLimiter:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # No release; the rolling window expires naturally.
        return None

    def available(self) -> int:
        """Return the current number of free slots (non-blocking, approximate).

        Does not take the lock; the result can be stale the instant it's
        returned. Intended for metrics / diagnostics, not for gating.
        """
        now = time.monotonic()
        self._prune(now)
        return max(0, self._max - len(self._calls))
