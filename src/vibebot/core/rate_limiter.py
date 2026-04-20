"""Token-bucket outgoing-message throttle shared by one IRC network connection.

IRC servers apply per-connection flood protection; a single bucket per network
matches that shape. Defaults mirror irssi (CMDS_MAX_AT_ONCE=5, CMD_QUEUE_SPEED=2200ms).
"""

from __future__ import annotations

import asyncio
import time


class BucketOverflow(RuntimeError):
    """Raised when the pending-waiter queue exceeds `max_pending`."""


class TokenBucket:
    def __init__(
        self,
        burst: int,
        period: float,
        *,
        enabled: bool = True,
        max_pending: int = 256,
        clock: callable = time.monotonic,
    ) -> None:
        if burst < 1:
            raise ValueError("burst must be >= 1")
        if period <= 0:
            raise ValueError("period must be > 0")
        self._burst = burst
        self._period = period
        self._enabled = enabled
        self._max_pending = max_pending
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._pending = 0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def burst(self) -> int:
        return self._burst

    @property
    def period(self) -> float:
        return self._period

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self._tokens = min(float(self._burst), self._tokens + elapsed / self._period)
        self._last = now

    async def acquire(self) -> None:
        if not self._enabled:
            return
        if self._pending >= self._max_pending:
            raise BucketOverflow(
                f"rate-limit queue full ({self._pending}/{self._max_pending})"
            )
        self._pending += 1
        try:
            async with self._lock:
                while True:
                    self._refill()
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) * self._period
                    await asyncio.sleep(wait)
        finally:
            self._pending -= 1

    def update(
        self,
        *,
        burst: int | None = None,
        period: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        if burst is not None:
            if burst < 1:
                raise ValueError("burst must be >= 1")
            self._burst = burst
            self._tokens = min(self._tokens, float(burst))
        if period is not None:
            if period <= 0:
                raise ValueError("period must be > 0")
            self._period = period
        if enabled is not None:
            self._enabled = enabled
