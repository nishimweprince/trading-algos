"""SPEC §8.1: incremental bars and indicators equal lib.py (pandas) on the same data."""

import numpy as np
import pandas as pd
import pytest
from conftest import research_minutes
from lib import adx, atr, ema, resample, rsi

from walkforward_patterns.bars import Aggregator, day_aggregator
from walkforward_patterns.indicators import Adx, Atr, Ewm, Rsi


def build(m1: pd.DataFrame, minutes: int, offset: int = 0) -> pd.DataFrame:
    agg = Aggregator(minutes, offset)
    rows = []
    for t, o, h, l, c in zip(research_minutes(m1.index), m1.o, m1.h, m1.l, m1.c, strict=True):
        b = agg.flush_until(int(t))
        if b is not None:
            rows.append(b)
        b = agg.add(int(t), o, h, l, c)
        assert b is None
    rows.append(agg.cur)
    idx = pd.DatetimeIndex(np.array([b.start for b in rows], dtype="datetime64[m]").astype("datetime64[ns]"))
    return pd.DataFrame({"o": [b.o for b in rows], "h": [b.h for b in rows], "l": [b.l for b in rows],
                         "c": [b.c for b in rows]}, index=idx)


@pytest.mark.parametrize("rule,minutes", [("5min", 5), ("10min", 10), ("15min", 15), ("1h", 60), ("4h", 240)])
def test_bars_equal_resample(m1, rule, minutes):
    pd.testing.assert_frame_equal(build(m1, minutes), resample(m1, rule), check_freq=False, check_dtype=False)


def test_trading_day_bars(m1):
    shifted = m1.copy()
    shifted.index = shifted.index + pd.Timedelta(hours=7)
    ref = resample(shifted, "1D")
    ours = build(m1, 1440, offset=420)
    ours.index = ours.index + pd.Timedelta(hours=7)
    pd.testing.assert_frame_equal(ours, ref, check_freq=False, check_dtype=False)
    assert day_aggregator().bucket(0) == -420


def _check(ours, ref):
    ref = np.asarray(ref, dtype=float)
    ours = np.asarray(ours, dtype=float)
    assert np.array_equal(np.isnan(ours), np.isnan(ref))
    f = ~np.isnan(ref)
    np.testing.assert_allclose(ours[f], ref[f], rtol=0, atol=1e-9)


@pytest.mark.parametrize("which", ["synthetic", "real"])
def test_indicators_equal_lib(request, which):
    m1 = request.getfixturevalue("m1" if which == "synthetic" else "real_m1")
    for rule in ("15min", "1h"):
        d = resample(m1, rule)
        e50, e20, w = Ewm.span(50), Ewm.span(20), Ewm.wilder(14)
        a14, a100, r2, r14, ad = Atr(14), Atr(100), Rsi(2), Rsi(14), Adx(14)
        cols = {k: [] for k in ("e50", "e20", "w", "a14", "a100", "r2", "r14", "adx")}
        for h, l, c in zip(d.h, d.l, d.c, strict=True):
            cols["e50"].append(e50.update(c)); cols["e20"].append(e20.update(c))  # noqa: E702
            cols["w"].append(w.update(h - l)); cols["a14"].append(a14.update(h, l, c))  # noqa: E702
            cols["a100"].append(a100.update(h, l, c)); cols["r2"].append(r2.update(c))  # noqa: E702
            cols["r14"].append(r14.update(c)); cols["adx"].append(ad.update(h, l, c))  # noqa: E702
        _check(cols["e50"], ema(d.c, 50))
        _check(cols["e20"], ema(d.c, 20))
        _check(cols["w"], (d.h - d.l).ewm(alpha=1 / 14, adjust=False).mean())
        _check(cols["a14"], atr(d, 14))
        _check(cols["a100"], atr(d, 100))
        _check(cols["r2"], rsi(d.c, 2))
        _check(cols["r14"], rsi(d.c, 14))
        _check(cols["adx"], adx(d, 14))


def test_ewm_mid_nan_matches_pandas():
    x = np.array([1.0, np.nan, 3.0, np.nan, np.nan, 2.5, 4.0, np.nan, 7.0])
    e = Ewm.wilder(3)
    _check([e.update(v) for v in x], pd.Series(x).ewm(alpha=1 / 3, adjust=False).mean())
    x = np.r_[np.nan, np.nan, x]
    e = Ewm.span(5)
    _check([e.update(v) for v in x], pd.Series(x).ewm(span=5, adjust=False).mean())
