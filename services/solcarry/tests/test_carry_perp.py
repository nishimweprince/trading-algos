from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solcarry.analytics.carry_perp import (
    funding_diagnostics,
    hourly_carry,
    perp_cost,
    rolling_windows,
    run_perp,
)
from solcarry.analytics.stats import longest_underwater_days

IDX = pd.date_range("2025-04-01", periods=24 * 120, freq="h", tz="UTC")


def _const(v):
    return pd.Series(v, index=IDX)


def test_cost(cfg):
    assert perp_cost(cfg) == pytest.approx((20 / 0.5 + 10) / 1e4)


def test_constant_analytic():
    y, fa, rf, beta, k, c = 0.05, 0.10, 0.04, 0.2, 1.3, 0.005
    h = hourly_carry(_const(fa / 8760), _const(y), _const(rf), 0.0, beta, k, c)
    assert h["R_h"].iloc[0] == pytest.approx((y + fa - c) / k)
    w = rolling_windows(h, [30])
    assert w["R_W"].iloc[0] == pytest.approx((y + fa - c) / k)
    assert w["X_W"].iloc[0] == pytest.approx((y + fa - c) / k - rf)


def test_k_dilution_and_reserve_yield():
    h0 = hourly_carry(_const(0.0), _const(0.05), _const(0.0), 0.0, 0.5, 1.6, 0.0)
    h1 = hourly_carry(_const(0.0), _const(0.05), _const(0.0), _const(0.04), 0.5, 1.6, 0.0)
    assert h0["R_h"].iloc[0] == pytest.approx(0.05 / 1.6)
    assert h1["R_h"].iloc[0] == pytest.approx((0.05 + 0.5 * 0.04) / 1.6)


def test_windows_step_and_coverage():
    h = hourly_carry(_const(0.0), _const(0.05), _const(0.0), 0.0, 0.0, 1.0, 0.0)
    w = rolling_windows(h, [7, 30])
    assert (w[w.window_days == 7]["end"].diff().dropna() == pd.Timedelta(days=1)).all()
    assert len(w[w.window_days == 30]) == 120 - 30 + 1
    h2 = h.drop(h.index[24 * 10 : 24 * 15])  # 5 missing days
    w2 = rolling_windows(h2, [7])
    assert len(w2) < len(w[w.window_days == 7])


def test_run_perp_end_to_end(cfg):
    funding = pd.DataFrame({"f_h": _const(0.10 / 8760)})
    staking = pd.DataFrame({"y_lst": _const(0.05), "y_native": _const(0.052)})
    hurdle = pd.DataFrame({"r_f": _const(0.04)})
    close = pd.Series(np.linspace(100, 150, len(IDX)), index=IDX)
    res = run_perp(cfg, "P-LST-BIN", funding, staking, hurdle, pd.DataFrame(), close, 0.02)
    expect = (0.05 + 0.10 - perp_cost(cfg)) / (1 + 0.10 + 1.05 * 0.02) - 0.04
    assert res.summary["excess"]["median"] == pytest.approx(expect)
    assert res.summary["share_negative"] == 0
    assert res.summary["flags"] == ["PROXY"]
    assert res.summary["sample_days"] == pytest.approx(120, abs=0.1)


def test_underwater():
    idx = pd.date_range("2025-01-01", periods=24 * 10, freq="h", tz="UTC")
    x = pd.Series(0.1, index=idx)
    x.iloc[24:72] = -0.2  # 2 days negative, then recover over 4 days
    d = longest_underwater_days(x)
    assert 5.5 < d < 6.5
    assert longest_underwater_days(pd.Series(0.1, index=idx)) == 0


def test_funding_diag():
    idx = pd.date_range("2025-01-01", periods=24 * 30, freq="h", tz="UTC")
    f = pd.Series(1e-5, index=idx)
    f.iloc[: 24 * 10] = -1e-5
    d = funding_diagnostics(f, "x")
    assert d["share_positive"] == pytest.approx(2 / 3)
    assert d["longest_negative_run_days_7d"] > 5
