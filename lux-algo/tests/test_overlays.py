from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lux_algo.candles import Candle
from lux_algo.overlays import (
    ha_bias_dir,
    psar_dir,
    range_filter,
    range_filter_dir,
    reversals,
    smart_trail_dir,
    superichi_dir,
    tbo_dir,
    tp_points,
)


def _trend(n: int, slope: float, start: float = 100.0) -> list[Candle]:
    candles: list[Candle] = []
    for i in range(n):
        close = start + slope * i
        candles.append(
            Candle(
                start=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=close - slope,
                high=close + 0.3,
                low=close - 0.3,
                close=close,
                volume=1.0,
            )
        )
    return candles


DIR_FUNCS = [range_filter_dir, superichi_dir, tbo_dir, smart_trail_dir, ha_bias_dir, psar_dir]


def test_all_direction_overlays_agree_with_trend() -> None:
    up = _trend(200, 0.5)
    down = _trend(200, -0.5)
    for fn in DIR_FUNCS:
        assert fn(up)[-1] == 1, fn.__name__
        assert fn(down)[-1] == -1, fn.__name__


def test_range_filter_fdir_is_ternary() -> None:
    filt, fdir = range_filter(_trend(120, 0.5))
    assert set(fdir) <= {-1, 0, 1}
    assert fdir[-1] == 1
    assert len(filt) == 120


def test_tp_points_flags_exhaustion_bottom_as_buy() -> None:
    # Long steady decline (>qual bars of close<close[4]) then a green bar at the low
    # should mark a major BUY TP (1).
    candles: list[Candle] = []
    for i in range(60):
        close = 200.0 - i  # steady decline, sindex accumulates
        candles.append(
            Candle(
                start=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=close + 0.5,  # red bars on the way down
                high=close + 1.0,
                low=close - 0.5,
                close=close,
                volume=1.0,
            )
        )
    # Final bar: green (close>open) making a new low over the window.
    last = candles[-1]
    candles[-1] = Candle(
        last.start,
        open=last.close - 2.0,
        high=last.close + 0.2,
        low=min(c.low for c in candles) - 1.0,
        close=last.close,
        volume=1.0,
    )
    assert tp_points(candles, qual=13, length=40)[-1] == 1


def test_reversals_return_bool_series() -> None:
    revup, revdn = reversals(_trend(80, 0.5))
    assert len(revup) == 80 and len(revdn) == 80
    assert all(isinstance(x, bool) for x in revup)
