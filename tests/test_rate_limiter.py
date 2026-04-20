"""Unit tests for core.rate_limiter.TokenBucket."""

from __future__ import annotations

import asyncio
import time

import pytest

from vibebot.core.rate_limiter import BucketOverflow, TokenBucket


async def test_burst_then_throttle():
    bucket = TokenBucket(burst=5, period=0.1)
    start = time.monotonic()
    # 5 in the burst: instant. 5 more: spaced by ~period.
    for _ in range(10):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5, f"expected >=0.5s for 10 acquires, got {elapsed:.3f}"
    assert elapsed < 1.5, f"expected <1.5s, got {elapsed:.3f}"


async def test_disabled_is_noop():
    bucket = TokenBucket(burst=1, period=10.0, enabled=False)
    start = time.monotonic()
    for _ in range(100):
        await bucket.acquire()
    assert time.monotonic() - start < 0.1


async def test_overflow_raises():
    bucket = TokenBucket(burst=1, period=60.0, max_pending=4)
    # First acquire consumes the one token; subsequent acquires queue.
    await bucket.acquire()

    tasks = [asyncio.create_task(bucket.acquire()) for _ in range(4)]
    await asyncio.sleep(0.05)  # let them park inside acquire()

    with pytest.raises(BucketOverflow):
        await bucket.acquire()

    for t in tasks:
        t.cancel()
    for t in tasks:
        with pytest.raises((asyncio.CancelledError, BucketOverflow)):
            await t


async def test_update_live():
    bucket = TokenBucket(burst=1, period=10.0)
    await bucket.acquire()  # drains the burst
    bucket.update(burst=10, period=0.01, enabled=True)
    start = time.monotonic()
    # Refill rate is fast now; acquires should complete quickly.
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.5


async def test_update_disable_flips_noop():
    bucket = TokenBucket(burst=1, period=60.0)
    await bucket.acquire()
    bucket.update(enabled=False)
    start = time.monotonic()
    for _ in range(20):
        await bucket.acquire()
    assert time.monotonic() - start < 0.05


def test_validation():
    with pytest.raises(ValueError):
        TokenBucket(burst=0, period=1.0)
    with pytest.raises(ValueError):
        TokenBucket(burst=1, period=0)
    b = TokenBucket(burst=1, period=1.0)
    with pytest.raises(ValueError):
        b.update(burst=0)
    with pytest.raises(ValueError):
        b.update(period=-1.0)
