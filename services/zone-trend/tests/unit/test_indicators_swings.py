from __future__ import annotations

import numpy as np
import pytest

from zone_trend.core.indicators import (
    Ema,
    RollingMedian,
    Wilder,
    atr_series,
    ema_series,
    pearson,
    volume_profile_hvn,
)
from zone_trend.core.series import BarSeries
from zone_trend.core.swings import SwingTracker
from zone_trend.types import TF, Bar

H = 3_600_000


def random_ohlc(n: int, seed: int = 1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    return open_, high, low, close


def test_incremental_atr_and_ema_match_vectorised_reference():
    _, high, low, close = random_ohlc(500)
    w, e = Wilder(14), Ema(50)
    inc_atr = np.array([w.update(h, lo, c) for h, lo, c in zip(high, low, close, strict=True)])
    inc_ema = np.array([e.update(c) for c in close])
    ref_atr, ref_ema = atr_series(high, low, close, 14), ema_series(close, 50)
    assert np.isnan(inc_atr[:13]).all() and np.isnan(ref_atr[:13]).all()
    np.testing.assert_allclose(inc_atr[13:], ref_atr[13:], rtol=0, atol=1e-9)
    np.testing.assert_allclose(inc_ema[49:], ref_ema[49:], rtol=0, atol=1e-9)


def test_rolling_median_and_pearson():
    m = RollingMedian(3)
    assert np.isnan(m.update(1.0)) and np.isnan(m.update(5.0))
    assert m.update(3.0) == 3.0 and m.update(10.0) == 5.0
    x = np.arange(10.0)
    assert pearson(x, 2 * x + 1) == pytest.approx(1.0)
    assert pearson(x, -x) == pytest.approx(-1.0)


def test_hvn_finds_the_heavy_price_level():
    rng = np.random.default_rng(0)
    centre = rng.normal(100, 0.3, 400)
    high = np.concatenate([centre + 0.2, np.linspace(90, 110, 100) + 0.2])
    low = np.concatenate([centre - 0.2, np.linspace(90, 110, 100) - 0.2])
    vol = np.concatenate([np.full(400, 10.0), np.full(100, 1.0)])
    nodes = volume_profile_hvn(high, low, vol, bins=40)
    assert nodes and min(abs(n - 100) for n in nodes) < 1.0


def series_from(highs: list[float], lows: list[float], tf: TF = TF.H1) -> BarSeries:
    s = BarSeries(tf)
    for i, (h, lo) in enumerate(zip(highs, lows, strict=True)):
        mid = (h + lo) / 2
        s.append(Bar(i * H, (i + 1) * H - 1, mid, h, lo, mid, 1.0))
    return s


def feed(highs, lows, left=3, right=3, k=0.0):
    tracker = SwingTracker(left, right, k)
    s = BarSeries(TF.H1)
    confirmed_at = []
    for i, (h, lo) in enumerate(zip(highs, lows, strict=True)):
        mid = (h + lo) / 2
        s.append(Bar(i * H, (i + 1) * H - 1, mid, h, lo, mid, 1.0))
        for sw in tracker.update(s):
            confirmed_at.append((i, sw))
    return tracker, confirmed_at


# 14 flat bars warm the ATR up, then a clean peak at index 18 and a trough at 24.
BASE_H = [101.0] * 14 + [102, 103, 104, 105, 110, 105, 104, 103, 102, 101, 95, 101, 102, 103]
BASE_L = [99.0] * 14 + [100, 101, 102, 103, 108, 103, 102, 101, 100, 99, 90, 99, 100, 101]


def test_swing_is_confirmed_exactly_right_bars_after_the_pivot():
    tracker, confirmed = feed(BASE_H, BASE_L)
    highs = [(i, sw) for i, sw in confirmed if sw.kind == "high"]
    assert highs[0][0] == 18 + 3
    sw = highs[0][1]
    assert sw.price == 110 and sw.pivot_time == 18 * H and sw.t_conf == (21 + 1) * H - 1
    assert tracker.visible(sw.t_conf - 1) == [s for s in tracker.swings if s.t_conf < sw.t_conf]


def test_leg_filter_rejects_small_pivots():
    _, loose = feed(BASE_H, BASE_L, k=0.0)
    _, strict = feed(BASE_H, BASE_L, k=50.0)
    assert loose and not strict


def test_same_kind_swings_collapse_to_the_more_extreme_with_later_t_conf():
    # Two peaks with no confirmable low between them: 110 then 115.
    highs = [101.0] * 14 + [105, 110, 105, 104, 105, 106, 115, 106, 105, 104, 103]
    lows = [99.0] * 14 + [103, 108, 103, 102.5, 103, 104, 113, 104, 103, 102, 101]
    tracker, confirmed = feed(highs, lows)
    kinds = [s.kind for s in tracker.swings]
    assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))
    top = [s for s in tracker.swings if s.kind == "high"][-1]
    assert top.price == 115
    first_conf = next(sw for _, sw in confirmed if sw.price == 110).t_conf
    assert top.t_conf > first_conf
