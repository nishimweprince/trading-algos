from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lux_algo.candles import Candle
from lux_algo.indicators import (
    atr,
    crossover,
    crossunder,
    rma,
    sma,
    supertrend,
    true_range,
)


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
    assert out[2] == 2.0  # sma of first 3
    assert abs(out[3] - (4 / 3 + 2 / 3 * 2.0)) < 1e-12
    assert abs(out[4] - (5 / 3 + 2 / 3 * out[3])) < 1e-12


def test_true_range_first_bar_is_high_low() -> None:
    candles = [_candle(10, 12, 9, 11, 0), _candle(11, 13, 10, 12, 1)]
    tr = true_range(candles)
    assert tr[0] == 3.0  # 12 - 9
    # max(13-10, |13-11|, |10-11|) = max(3, 2, 1) = 3
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
    # None inputs never cross.
    assert crossover([None, 3], [2, 2], 1) is False


def test_supertrend_direction_in_persistent_trends() -> None:
    up = [_candle(100 + i, 100.5 + i, 99.5 + i, 100 + i, i) for i in range(60)]
    st, direction = supertrend(up, factor=2.0, atr_len=11)
    assert direction[-1] == -1  # up-trend
    assert st[-1] is not None and st[-1] < up[-1].close

    down = [_candle(100 - i, 100.5 - i, 99.5 - i, 100 - i, i) for i in range(60)]
    st_d, direction_d = supertrend(down, factor=2.0, atr_len=11)
    assert direction_d[-1] == 1  # down-trend
    assert st_d[-1] is not None and st_d[-1] > down[-1].close
