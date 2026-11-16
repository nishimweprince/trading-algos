import numpy as np
import pandas as pd

from crypto_trend.core import monthly_universe, portfolio, slot_states, weights
from crypto_trend.fetch import eligible_symbol


def test_eligible_symbol():
    assert eligible_symbol("BTCUSDT") and eligible_symbol("LUNAUSDT")
    assert not eligible_symbol("USDCUSDT") and not eligible_symbol("BTCUPUSDT")
    assert not eligible_symbol("ETHBTC") and not eligible_symbol("PAXGUSDT")


def test_slot_breakout_and_ratcheting_stop():
    c = np.array([10, 10, 10, 12, 13, 14, 11.5, 9.0, 9.0])
    s = pd.Series(c)
    hi = s.shift(1).rolling(3).max().to_numpy()
    lo = s.shift(1).rolling(3).min().to_numpy()
    st = slot_states(c, hi, lo, np.ones(len(c), bool))
    # breakout at t=3 (12 > 10), stop rises to mid of (13,14..), exits when close < stop
    assert list(st[:6]) == [0, 0, 0, 1, 1, 1]
    assert st[7] == 0  # 9.0 below the ratcheted stop
    off = slot_states(c, hi, lo, np.zeros(len(c), bool))
    assert off.sum() == 0


def test_universe_needs_history_and_volume():
    idx = pd.date_range("2020-01-01", "2021-03-01", freq="D")
    close = pd.DataFrame({"A": 1.0, "B": 1.0}, index=idx)
    close.loc[:"2020-12-01", "B"] = np.nan  # B too young in Feb 2021
    qvol = pd.DataFrame({"A": 3e6, "B": 9e6}, index=idx)
    m = monthly_universe(close, qvol)
    assert m.loc["2021-02-01", "A"] and not m.loc["2021-02-01", "B"]
    qvol["A"] = 1e6
    assert not monthly_universe(close, qvol).loc["2021-02-01", "A"]  # below $2M median


def test_weights_cap_and_portfolio_costs():
    idx = pd.date_range("2020-01-01", periods=600, freq="D")
    rng = np.random.default_rng(1)
    close = pd.DataFrame({"A": 100 * np.exp(np.cumsum(0.002 + 0.001 * rng.standard_normal(600)))},
                         index=idx)
    member = pd.DataFrame(True, index=idx, columns=["A"])
    w = weights(close, member, n_slots_coins=1)
    assert w.A.max() <= 2.0 + 1e-12  # 9 slots x (1/9) x cap 2
    assert w.A.iloc[-1] > 1.9  # steady low-vol uptrend: all slots long at the cap
    p0, p1 = portfolio(close, w, 0.0), portfolio(close, w, 0.001)
    assert np.allclose(p0.gross, p1.gross)
    assert np.allclose(p0.net - p1.net, 0.001 * p0.turnover)
