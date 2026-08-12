from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ipda.candles import Candle
from ipda.indicators import (
    atr,
    crossover,
    crossunder,
    rma,
    rsi,
    sma,
    supertrend,
    true_range,
)

# Wilder's own worked example (New Concepts in Technical Trading Systems, 1978).
# The first RSI(14) reading on this series is the published 70.46.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


def _candle(o: float, h: float, low: float, c: float, i: int = 0) -> Candle:
    return Candle(
        start=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=0.0,
    )


def test_sma_basic() -> None:
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_rma_seeds_with_sma_then_wilder_recursion() -> None:
    out = rma([1, 2, 3, 4, 5], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0
    assert abs(out[3] - (4 / 3 + 2 / 3 * 2.0)) < 1e-12
    assert abs(out[4] - (5 / 3 + 2 / 3 * out[3])) < 1e-12


def test_true_range_first_bar_is_high_low() -> None:
    candles = [_candle(10, 12, 9, 11, 0), _candle(11, 13, 10, 12, 1)]
    tr = true_range(candles)
    assert tr[0] == 3.0
    assert tr[1] == 3.0


def test_atr_matches_rma_of_true_range() -> None:
    candles = [_candle(10 + i, 11 + i, 9 + i, 10 + i, i) for i in range(10)]
    assert atr(candles, 5) == rma(true_range(candles), 5)


def test_crossover_and_crossunder() -> None:
    a: list[float | None] = [1, 2, 3]
    b: list[float | None] = [2, 2, 2]
    assert crossover(a, b, 2) is True
    assert crossover(a, b, 1) is False
    assert crossunder([3, 2, 1], [2, 2, 2], 2) is True
    assert crossover([None, 3], [2, 2], 1) is False


def test_supertrend_direction_in_persistent_trends() -> None:
    up = [_candle(100 + i, 100.5 + i, 99.5 + i, 100 + i, i) for i in range(60)]
    st, direction = supertrend(up, factor=2.0, atr_len=11)
    assert direction[-1] == -1
    assert st[-1] is not None and st[-1] < up[-1].close

    down = [_candle(100 - i, 100.5 - i, 99.5 - i, 100 - i, i) for i in range(60)]
    st_d, direction_d = supertrend(down, factor=2.0, atr_len=11)
    assert direction_d[-1] == 1
    assert st_d[-1] is not None and st_d[-1] > down[-1].close


def test_rsi_matches_wilders_published_value() -> None:
    values = rsi(WILDER_CLOSES, 14)

    # 14 changes need 15 closes, so index 14 is the first defined reading.
    assert values[13] is None
    assert abs(values[14] - 70.4641) < 1e-4
    assert abs(values[15] - 66.2496) < 1e-4


def test_rsi_is_none_until_the_seed_bar() -> None:
    values = rsi(WILDER_CLOSES, 14)

    assert all(v is None for v in values[:14])
    assert all(v is not None for v in values[14:])


def test_rsi_handles_short_and_empty_input() -> None:
    assert rsi([], 14) == []
    assert rsi([1.0], 14) == [None]


def test_rsi_guards_reproduce_pine_precedence() -> None:
    """file.txt checks ``dnwardd == 0`` first, so a flat series reports 100, not 50.

    That is the Pine's behaviour, odd as it looks, and the port matches it rather
    than silently 'fixing' the indicator.
    """
    rising = [float(i) for i in range(20)]
    falling = [float(20 - i) for i in range(20)]
    flat = [5.0] * 20

    assert rsi(rising, 14)[19] == 100.0
    assert rsi(falling, 14)[19] == 0.0
    assert rsi(flat, 14)[19] == 100.0
