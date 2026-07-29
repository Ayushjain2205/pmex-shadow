import asyncio
import time

import pytest

from pmex_shadow.execution.ratelimit import TokenBucket


async def test_acquire_never_drops_just_waits():
    bucket = TokenBucket(rate_per_minute=600, burst=2)  # 10/s, burst 2
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # 2 free from burst, 2 more at 10/s => ~0.2s minimum wait, generous upper bound
    assert elapsed >= 0.15
    assert elapsed < 2.0


async def test_burst_capacity_is_immediate():
    bucket = TokenBucket(rate_per_minute=60, burst=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


async def test_concurrent_acquires_all_eventually_succeed():
    bucket = TokenBucket(rate_per_minute=600, burst=1)
    results = await asyncio.gather(*[bucket.acquire() for _ in range(5)])
    assert len(results) == 5
