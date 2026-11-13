import numpy as np
import pandas as pd

from fuzzy_osc.core import backtest, memberships, rsi, signal, stoch_main, system_vote


def _bars(close: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1h", tz="UTC")
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c + 0.01,
                         "low": c - 0.01, "close": c})


def test_memberships_edges():
    b, s, n = memberships(np.array([20.0, 30.0, 50.0, 70.0, 80.0]), "rsi")
    assert np.allclose(b, [1, 0.5, 0, 0, 0])
    assert np.allclose(s, [0, 0, 0, 0.5, 1])
    assert np.allclose(n, [0, 0.5, 1, 0.5, 0])


def test_rsi_extremes():
    up = rsi(pd.Series(np.arange(1, 60, dtype=float)), 14)
    assert up.iloc[-1] > 99


def test_system_vote_all_oversold_is_buy():
    s = pd.Series([10.0, 50.0, 90.0])
    v = system_vote([s, s, s], "rsi")
    assert list(v) == [1, 0, -1]


def test_falling_market_gives_long_signal_and_sign_flip():
    close = np.concatenate([np.linspace(100, 90, 80), np.linspace(90, 95, 40)])
    bars = _bars(close)
    sig = signal(bars, "fuzzy")
    assert (sig[:80] == 1).any() and not (sig[:80] == -1).any()
    t = backtest(bars, sig, 4, 0.0)
    r = backtest(bars, sig, 4, 0.0, sign=-1)
    assert np.allclose(t.net_bps.to_numpy(), -r.net_bps.to_numpy())


def test_backtest_one_position_and_cost():
    bars = _bars(np.linspace(100, 101, 30))
    sig = np.zeros(30, dtype=int)
    sig[[2, 3, 4]] = 1  # 3 and 4 fall inside the first trade
    t = backtest(bars, sig, 5, cost=0.1)
    assert len(t) == 1  # sig[3], sig[4] fall inside the open trade
    e, x = bars.open.iloc[3], bars.open.iloc[8]
    assert np.isclose(t.net_bps.iloc[0], (x - e - 0.1) / e * 1e4)


def test_stoch_bounds():
    bars = _bars(100 + np.sin(np.arange(200) / 5))
    s = stoch_main(bars, 14, 3).dropna()
    assert s.between(0, 100).all()
