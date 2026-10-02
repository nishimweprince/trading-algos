from __future__ import annotations

import pytest

from ta_plugin_binance_futures.limiter import OrderRateGovernor, WeightLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_governor_caps_at_fraction_of_both_windows() -> None:
    clock = Clock()
    governor = OrderRateGovernor(0.5, clock=clock)
    assert governor.caps == ((10.0, 150), (60.0, 600))
    for _ in range(150):
        assert governor.try_acquire()
    assert not governor.try_acquire()
    clock.now = 10.0  # first 10 s window has rolled
    assert governor.try_acquire()


def test_governor_minute_window_binds() -> None:
    clock = Clock()
    governor = OrderRateGovernor(0.5, clock=clock)
    sent = 0
    for second in range(0, 60, 10):
        clock.now = float(second)
        while governor.try_acquire():
            sent += 1
    assert sent == 600
    clock.now = 59.9
    assert governor.headroom() == 0
    clock.now = 60.0
    assert governor.headroom() == 150


def test_governor_rejects_bad_fraction() -> None:
    with pytest.raises(ValueError):
        OrderRateGovernor(0)


async def test_weight_limiter_adopts_server_count_and_blocks() -> None:
    clock = Clock()
    limiter = WeightLimiter(100, clock=clock)
    await limiter.acquire(10)
    limiter.observe(80)
    assert limiter.used == 80
    limiter.block(30)
    assert limiter.blocked_for() == 30
    clock.now = 61
    assert limiter.used == 0
