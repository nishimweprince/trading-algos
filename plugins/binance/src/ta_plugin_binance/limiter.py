"""Client-side request-weight budgeting for the Binance REST API.

Binance meters REST usage per IP in "weight" per rolling minute, reports the
running total in ``X-MBX-USED-WEIGHT-1M``, answers 429 when it is exceeded and
418 (an IP ban) when 429s are ignored. This limiter keeps the process under a
configured budget, adopts the server's count whenever it is reported (other
processes on the same IP spend from the same budget), and stops all calls
until ``Retry-After`` once Binance has said stop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

__all__ = ["WeightLimiter"]

WINDOW_SECONDS = 60.0


class WeightLimiter:
    def __init__(
        self,
        budget: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.budget = budget
        self._clock = clock
        self._sleep = sleep
        self._window_start = clock()
        self._used = 0
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    def blocked_for(self) -> float:
        """Seconds until Binance allows calls again after a 429/418, else 0."""
        return max(0.0, self._blocked_until - self._clock())

    def _roll(self) -> None:
        now = self._clock()
        if now - self._window_start >= WINDOW_SECONDS:
            self._window_start = now
            self._used = 0

    async def acquire(self, weight: int) -> None:
        """Wait until ``weight`` fits in the budget, then spend it."""
        async with self._lock:
            while True:
                self._roll()
                if self._used + weight <= self.budget:
                    self._used += weight
                    return
                wait = WINDOW_SECONDS - (self._clock() - self._window_start)
                await self._sleep(max(wait, 0.0))  # type: ignore[misc]

    def observe(self, used_weight: int | None) -> None:
        """Adopt the server's count for the current window."""
        if used_weight is not None:
            self._roll()
            self._used = max(self._used, used_weight)

    def block(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)
