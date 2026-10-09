from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from zone_trend.backtest.montecarlo import monte_carlo, trade_returns
from zone_trend.backtest.walkforward import (
    GRID_MULTS,
    PARAMS,
    candidates,
    holdout_range,
    neighbours,
    plan_windows,
    score,
)
from zone_trend.config import load_config

CFG = load_config()
DAY = 86_400_000


def test_windows_step_six_months_and_stop_before_the_holdout():
    ws = plan_windows("2021-01", "2026-09")
    assert holdout_range("2026-09") == ("2026-01", "2026-09")
    assert ws[0].is_start == "2021-01" and ws[0].is_end == "2022-06"
    assert ws[0].oos_start == "2022-07" and ws[0].oos_end == "2022-12"
    assert ws[1].is_start == "2021-07"
    assert ws[-1].oos_end <= "2025-12" and len(ws) == 7
    assert ws[0].folds() == [("2021-01", "2021-06"), ("2021-07", "2021-12"), ("2022-01", "2022-06")]


def test_grid_is_default_plus_valid_one_at_a_time_variants():
    cands = candidates(CFG)
    assert cands[0][0] == "default"
    assert len(cands) <= 1 + len(PARAMS) * len(GRID_MULTS)
    labels = {label for label, _ in cands}
    assert "exits.tp1_rx1.4" in labels
    # tp2 x0.6 = 1.8 still beats tp1 1.5; decay x1.4 = 0.84 is valid; nothing invalid sneaks in
    for _, c in cands:
        assert c.exits.tp2_r > c.exits.tp1_r
    assert len(neighbours(CFG)) <= len(PARAMS) * 2


def test_score_rewards_pf_and_count_and_penalises_drawdown():
    trades = pd.DataFrame({"net": [2.0, -1.0] * 8})
    flat = pd.DataFrame({"time": [0, 1], "equity": [100.0, 100.0]})
    deep = pd.DataFrame({"time": [0, 1, 2], "equity": [100.0, 80.0, 90.0]})
    assert score(trades, flat, 0.25) == pytest.approx(2.0 * 4)
    assert score(trades, deep, 0.25) == pytest.approx(2.0 * 4 * 0.2)
    assert score(pd.DataFrame(), flat, 0.25) == 0.0


def _trades(rets: list[float], equity: float = 100.0):
    times = np.arange(len(rets)) * DAY
    tr = pd.DataFrame({"entry_time": times + 1, "net": np.array(rets) * equity})
    eq = pd.DataFrame({"time": np.append(times, len(rets) * DAY), "equity": equity})
    return tr, eq


def test_trade_returns_use_equity_at_entry():
    tr, eq = _trades([0.01, -0.02])
    np.testing.assert_allclose(trade_returns(tr, eq), [0.01, -0.02])


def test_monte_carlo_constant_returns_have_no_spread():
    tr, eq = _trades([0.01] * 50)
    mc = monte_carlo(tr, eq, n_paths=200)
    assert mc["max_drawdown"][95] == 0.0 and mc["p_minus_50"] == 0.0
    assert mc["cagr"][5] == pytest.approx(mc["cagr"][95])


def test_monte_carlo_is_seeded_and_sees_ruin():
    tr, eq = _trades([0.05, -0.08] * 40)
    a = monte_carlo(tr, eq, n_paths=500)
    b = monte_carlo(tr, eq, n_paths=500)
    assert a == b
    assert a["p_minus_50"] > 0.5 and a["max_drawdown"][95] > 0.5
