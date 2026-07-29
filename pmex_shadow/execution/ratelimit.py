"""Token-bucket rate limiter ahead of the CLOB (FR-EXE-5). On saturation, callers
queue and wait — this never drops a request. Sized conservatively under the
per-account order-placement limits in docs/VERIFIED.md item 8 (5,000/10s burst,
120,000/10min sustained) — a single bot is nowhere near those, but `max_orders_per_minute`
(policy.yaml's own risk cap) is almost always the binding constraint in practice, not
the exchange's limit.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else rate_per_minute)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        self._last_refill = now

    async def acquire(self) -> None:
        """Blocks until a token is available. Never drops — callers queue naturally
        by awaiting this before submission."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                deficit = 1 - self._tokens
                wait_s = deficit / self.rate_per_second
            await asyncio.sleep(wait_s)
