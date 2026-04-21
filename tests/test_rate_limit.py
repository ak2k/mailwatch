"""Tests for :mod:`mailwatch.rate_limit`.

Uses ``pytest-asyncio`` in auto mode (``asyncio_mode = "auto"`` in
``pyproject.toml``). Windows are kept small (0.1-0.5s) so the suite
finishes quickly; no real network calls are made.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from mailwatch.rate_limit import RateLimiter


async def test_under_limit_is_instant() -> None:
    """5 acquires against max=5 must each complete in <10ms."""
    limiter = RateLimiter(max_per_window=5, window_seconds=1.0)
    start = time.monotonic()
    for _ in range(5):
        t0 = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.01, f"acquire took {elapsed * 1000:.2f}ms"
    total = time.monotonic() - start
    assert total < 0.05


async def test_over_limit_blocks() -> None:
    """With max=2 and window=0.5s, the 3rd acquire must take ~0.5s."""
    limiter = RateLimiter(max_per_window=2, window_seconds=0.5)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    mid = time.monotonic() - start
    assert mid < 0.05, f"first two acquires took {mid * 1000:.2f}ms"

    await limiter.acquire()
    total = time.monotonic() - start
    assert 0.45 <= total <= 0.7, f"3rd acquire total={total:.3f}s"


async def test_rolling_window() -> None:
    """After the window rolls past, old timestamps no longer count."""
    limiter = RateLimiter(max_per_window=2, window_seconds=0.2)
    t0 = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - t0 < 0.02

    # Wait for the window to roll past.
    await asyncio.sleep(0.25)

    t1 = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - t1 < 0.02


async def test_context_manager() -> None:
    """``async with limiter:`` calls acquire and exits cleanly."""
    limiter = RateLimiter(max_per_window=1, window_seconds=0.2)

    async with limiter as got:
        assert got is limiter
        assert limiter.available() == 0

    # Re-entering while window is still hot blocks briefly.
    t0 = time.monotonic()
    async with limiter:
        elapsed = time.monotonic() - t0
    assert 0.15 <= elapsed <= 0.35, f"re-enter elapsed={elapsed:.3f}s"


async def test_available_counts() -> None:
    """available() shrinks as slots fill and rebounds when the window rolls."""
    limiter = RateLimiter(max_per_window=3, window_seconds=0.2)
    assert limiter.available() == 3
    await limiter.acquire()
    assert limiter.available() == 2
    await limiter.acquire()
    assert limiter.available() == 1
    await limiter.acquire()
    assert limiter.available() == 0

    # After the window rolls past, slots come back.
    await asyncio.sleep(0.25)
    assert limiter.available() == 3


async def test_concurrent_fairness() -> None:
    """10 tasks against max=3 / window=0.2s — throughput matches the rate."""
    limiter = RateLimiter(max_per_window=3, window_seconds=0.2)
    completions: list[float] = []
    start = time.monotonic()

    async def worker() -> None:
        await limiter.acquire()
        completions.append(time.monotonic() - start)

    tasks = [asyncio.create_task(worker()) for _ in range(10)]
    await asyncio.gather(*tasks)

    # All 10 should complete. First 3 instant; then batches of ~3 every 0.2s.
    assert len(completions) == 10
    completions.sort()

    # First 3 are immediate (well under window).
    assert completions[2] < 0.05, f"first 3 completions: {completions[:3]}"

    # The 4th must wait ~one window past the 1st.
    assert 0.15 <= completions[3] <= 0.35, f"4th completion at {completions[3]:.3f}s"

    # 10 calls at 3-per-0.2s → total wall time ~0.6s (3 full windows).
    total = completions[-1]
    assert 0.55 <= total <= 0.95, f"total elapsed {total:.3f}s"


async def test_zero_max_raises() -> None:
    """``max_per_window < 1`` is a programming error, not a clamp."""
    with pytest.raises(ValueError, match="max_per_window must be >= 1"):
        RateLimiter(max_per_window=0)
    with pytest.raises(ValueError, match="max_per_window must be >= 1"):
        RateLimiter(max_per_window=-1)


async def test_invalid_window_raises() -> None:
    """Non-positive window is also a programming error."""
    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        RateLimiter(max_per_window=5, window_seconds=0.0)
    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        RateLimiter(max_per_window=5, window_seconds=-1.0)
