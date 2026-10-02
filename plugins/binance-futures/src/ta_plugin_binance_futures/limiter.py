"""Client-side budgets for the USDⓈ-M REST API and for order entry.

``WeightLimiter`` is the spot plugin's design (ta_plugin_binance.limiter),
repeated here rather than imported so the two plugins stay independent: keep
the process under a configured request weight per minute, adopt the server's
``X-MBX-USED-WEIGHT-1M`` count, and stop every call until ``Retry-After`` once
Binance has answered 429 or 418.

``OrderRateGovernor`` enforces the order-count limits (300 per 10 s and 1200
per minute per account) at a configured fraction. Nothing calls it until an
order path exists; it is here so that path cannot be built without it.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

__all__ = ["ORDER_LIMITS", "OrderRateGovernor", "WeightLimiter"]

WINDOW_SECONDS = 60.0

# (window seconds, orders allowed) per Binance USDⓈ-M account.
ORDER_LIMITS: tuple[tuple[float, int], ...] = ((10.0, 300), (60.0, 1200))


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


class OrderRateGovernor:
    """Sliding-window order counter at ``fraction`` of Binance's limits.

    ``try_acquire`` never waits: a scalper that has to queue an order behind
    the rate limit should not send it at all, so the caller gets a refusal.
    """

    def __init__(
        self,
        fraction: float = 0.5,
        *,
        limits: tuple[tuple[float, int], ...] = ORDER_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < fraction <= 1:
            raise ValueError("fraction must be in (0, 1]")
        self._windows = tuple((seconds, max(1, int(count * fraction))) for seconds, count in limits)
        self._clock = clock
        self._sent: deque[float] = deque()

    @property
    def caps(self) -> tuple[tuple[float, int], ...]:
        return self._windows

    def _prune(self, now: float) -> None:
        horizon = max(seconds for seconds, _ in self._windows)
        while self._sent and now - self._sent[0] >= horizon:
            self._sent.popleft()

    def headroom(self) -> int:
        """Orders that could be sent right now without breaching any window."""
        now = self._clock()
        self._prune(now)
        free = []
        for seconds, cap in self._windows:
            in_window = sum(1 for sent in self._sent if now - sent < seconds)
            free.append(cap - in_window)
        return max(0, min(free))

    def try_acquire(self) -> bool:
        if self.headroom() <= 0:
            return False
        self._sent.append(self._clock())
        return True
