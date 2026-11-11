from __future__ import annotations

import numpy as np
import pandas as pd

from solcarry.validate import check_convergence, check_velocity_floor, check_velocity_units


def _vel(rates, prices, cum_scale=1.0):
    cum = np.cumsum(rates) * cum_scale
    return pd.DataFrame(
        {
            "recordId": range(len(rates)),
            "fundingRate": rates,
            "fundingRateLong": rates,
            "cumulativeFundingRateLong": cum,
            "oraclePriceTwap": prices,
        }
    )


def test_velocity_units_ok():
    r = [0.0012] * 50
    assert check_velocity_units(_vel(r, [95.0] * 50), 1.5e-9).ok


def test_cum_diff_catches_misscaled_field():
    r = [0.0012] * 50
    c = check_velocity_units(_vel(r, [95.0] * 50, cum_scale=100.0), 1.5e-9)
    assert not c.ok and "cumulative" in c.detail


def test_unit_assumption_wrong():
    # fundingRate is actually a fraction; dividing by price annualizes to ~0 -> fine, so
    # construct the case where converted is out of band but raw is in band: tiny prices.
    r = [0.00002] * 50
    c = check_velocity_units(_vel(r, [0.0001] * 50), 1.5e-9)
    assert not c.ok and "UNIT ASSUMPTION WRONG" in c.detail


def _q(sym, exp, last_hour, F):
    idx = pd.date_range(last_hour - pd.Timedelta(hours=3), last_hour, freq="h")
    close = [F] * len(idx)
    if last_hour >= exp:  # bars after expiry with fresh prices = trading continued
        close = [F + 0.1 * k for k in range(len(idx))]
    return pd.DataFrame(
        {"ts": idx, "symbol": sym, "expiry_ts": exp, "close": close, "dte_days": 1.0}
    )


EXP = pd.Timestamp("2025-03-28 08:00", tz="UTC")
NOW = pd.Timestamp("2026-01-01", tz="UTC")
INDEX = pd.DataFrame(
    {"close": 100.0},
    index=pd.date_range(
        pd.Timestamp("2025-03-27", tz="UTC"), pd.Timestamp("2025-03-29", tz="UTC"), freq="h"
    ),
)


def test_hourly_fallback_ok_and_wrong_hour():
    one_h = pd.Timedelta(hours=1)
    ok = check_convergence(_q("S_250328", EXP, EXP - one_h, 100.05), INDEX, 0.002, NOW)
    assert ok.ok, ok.detail
    late = check_convergence(_q("S_250328", EXP, EXP + 5 * one_h, 100.05), INDEX, 0.002, NOW)
    assert not late.ok and "does not stop" in late.detail
    far = check_convergence(_q("S_250328", EXP, EXP - one_h, 101.0), INDEX, 0.002, NOW)
    assert not far.ok


def test_flat_settlement_bar_tolerated():
    q = _q("S_250328", EXP, EXP - pd.Timedelta(hours=1), 100.05)
    q = pd.concat([q, q.tail(1).assign(ts=EXP)], ignore_index=True)
    assert check_convergence(q, INDEX, 0.002, NOW).ok


def _m1(sym, exp, stop_at, f_dev):
    ts = pd.date_range(exp - pd.Timedelta(hours=2), exp + pd.Timedelta(minutes=59), freq="min")
    vol = [1.0 if t < stop_at else 0.0 for t in ts]
    return pd.DataFrame(
        {
            "ts": ts,
            "symbol": sym,
            "expiry_ts": exp,
            "f_close": 100 * (1 + f_dev),
            "f_volume": vol,
            "i_close": 100.0,
        }
    )


def test_minute_convergence():
    q = _q("S_250328", EXP, EXP - pd.Timedelta(hours=1), 105.0)  # hourly would fail
    good = check_convergence(q, INDEX, 0.005, NOW, _m1("S_250328", EXP, EXP, 0.001))
    assert good.ok, good.detail
    assert good.data["contracts"][0]["method"] == "1m"
    late = _m1("S_250328", EXP, EXP + pd.Timedelta(minutes=30), 0.001)
    assert "does not stop" in check_convergence(q, INDEX, 0.005, NOW, late).detail
    early = _m1("S_250328", EXP, EXP - pd.Timedelta(minutes=45), 0.001)
    assert not check_convergence(q, INDEX, 0.005, NOW, early).ok
    off = _m1("S_250328", EXP, EXP, 0.01)
    assert not check_convergence(q, INDEX, 0.005, NOW, off).ok


def test_floor_share():
    idx = pd.date_range("2026-07-01", periods=100, freq="h", tz="UTC")
    fv = pd.DataFrame({"f_h": 0.1095 / 8760}, index=idx)
    c = check_velocity_floor(fv, 0.1095)
    assert not c.ok and "PINNED" in c.detail
