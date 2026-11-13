"""Data loading, indicators, the fuzzy vote and a fixed-hold backtester (see PREREG.md)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA_ROOT = Path("/Users/pnishimwe/Documents/Markets/Data")
COST = {"AUDUSD": 0.00005, "USDJPY": 0.005, "XAUUSD": 0.30}  # round trip, price units

RSI_P = (9, 14, 21)
CCI_P = (9, 14, 21)
STO_P = ((5, 3, 3), (14, 7, 7), (21, 14, 14))
# (buy=1 at <=, buy=0 at >=, sell=0 at <=, sell=1 at >=)
MF = {"rsi": (25, 35, 65, 75), "cci": (-125, -75, 75, 125), "sto": (15, 25, 75, 85)}
RES_CUT = {"rsi": (0.4, 0.6), "cci": (0.4, 0.6), "sto": (0.2, 0.8)}
CRISP = {"rsi": (30, 70), "cci": (-100, 100), "sto": (20, 80)}


# ---------------------------------------------------------------- data
def load_m1(symbol: str, root: Path = DATA_ROOT, years: tuple[int, int] = (2020, 2026)
            ) -> pd.DataFrame:
    files = [f for f in sorted((root / symbol).glob(f"DAT_ASCII_{symbol}_M1_*.csv"))
             if years[0] <= int(f.stem.split("_")[-1][:4]) <= years[1]]
    if not files:
        raise FileNotFoundError(f"no {symbol} files in {root / symbol} for {years}")
    parts = []
    for f in files:
        df = pd.read_csv(f, sep=";", header=None, dtype={0: str},
                         names=["ts", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S").dt.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="NaT")
        parts.append(df.assign(ts=ts).dropna(subset=["ts"]).set_index("ts")
                     [["open", "high", "low", "close"]])
    out = pd.concat(parts)
    out.index = out.index.tz_convert("UTC")
    return out[~out.index.duplicated()].sort_index()


def resample(m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return m1.resample(rule, label="left", closed="left").agg(agg).dropna()


# ---------------------------------------------------------------- indicators
def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return out.mask((dn == 0) & (up > 0), 100.0)


def cci(bars: pd.DataFrame, n: int) -> pd.Series:
    tp = ((bars.high + bars.low + bars.close) / 3).to_numpy()
    out = np.full(len(tp), np.nan)
    if len(tp) >= n:
        w = np.lib.stride_tricks.sliding_window_view(tp, n)
        m = w.mean(axis=1)
        mad = np.abs(w - m[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n - 1:] = (tp[n - 1:] - m) / (0.015 * mad)
    return pd.Series(out, index=bars.index)


def stoch_main(bars: pd.DataFrame, k: int, slowing: int) -> pd.Series:
    """MT5 Stochastic main line: sum(C-LL)/sum(HH-LL) over `slowing` bars, x100."""
    ll = bars.low.rolling(k).min()
    hh = bars.high.rolling(k).max()
    num = (bars.close - ll).rolling(slowing).sum()
    den = (hh - ll).rolling(slowing).sum()
    return 100 * num / den.replace(0, np.nan)


def indicator_block(bars: pd.DataFrame) -> dict[str, list[pd.Series]]:
    return {
        "rsi": [rsi(bars.close, n) for n in RSI_P],
        "cci": [cci(bars, n) for n in CCI_P],
        "sto": [stoch_main(bars, k, s) for k, _d, s in STO_P],
    }


# ---------------------------------------------------------------- fuzzy
def memberships(x: np.ndarray, kind: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    b1, b0, s0, s1 = MF[kind]
    buy = np.clip((b0 - x) / (b0 - b1), 0, 1)
    sell = np.clip((x - s0) / (s1 - s0), 0, 1)
    return buy, sell, 1 - np.maximum(buy, sell)


def system_vote(inputs: list[pd.Series], kind: str) -> np.ndarray:
    """+1 buy, -1 sell, 0 neutral per bar for one indicator's Mamdani system."""
    mats = [memberships(s.to_numpy(float), kind) for s in inputs]
    b = np.minimum.reduce([m[0] for m in mats])
    s = np.minimum.reduce([m[1] for m in mats])
    n = np.minimum.reduce([m[2] for m in mats])
    tot = b + n + s
    with np.errstate(divide="ignore", invalid="ignore"):
        res = np.where(tot > 0, (0.2 * b + 0.5 * n + 0.8 * s) / tot, 0.5)
    lo, hi = RES_CUT[kind]
    out = np.where(res < lo, 1, np.where(res > hi, -1, 0))
    valid = np.all([np.isfinite(x.to_numpy(float)) for x in inputs], axis=0)
    return np.where(valid, out, 0)


def crisp_vote(inputs: list[pd.Series], kind: str) -> np.ndarray:
    lo, hi = CRISP[kind]
    a = np.vstack([x.to_numpy(float) for x in inputs])
    return np.where(np.all(a < lo, axis=0), 1, np.where(np.all(a > hi, axis=0), -1, 0))


def signal(bars: pd.DataFrame, variant: str = "fuzzy") -> np.ndarray:
    """+1 long / -1 short / 0 at each bar close: >= 2 of 3 systems agree, none opposes."""
    block = indicator_block(bars)
    f = system_vote if variant == "fuzzy" else crisp_vote
    votes = np.vstack([f(block[k], k) for k in ("rsi", "cci", "sto")])
    nb, ns = (votes == 1).sum(axis=0), (votes == -1).sum(axis=0)
    return np.where((nb >= 2) & (ns == 0), 1, np.where((ns >= 2) & (nb == 0), -1, 0))


# ---------------------------------------------------------------- backtest
def backtest(bars: pd.DataFrame, sig: np.ndarray, hold: int, cost: float,
             sign: int = 1) -> pd.DataFrame:
    """Enter at next open, exit at the open `hold` bars later; one position at a time."""
    o = bars.open.to_numpy(float)
    trades, i, n = [], 0, len(o)
    while i < n - hold - 1:
        d = int(sig[i]) * sign
        if d == 0:
            i += 1
            continue
        e, x = o[i + 1], o[i + 1 + hold]
        trades.append((bars.index[i + 1], d, e, x, (d * (x - e) - cost) / e * 1e4))
        i += hold  # next signal can be read on the bar before the exit open
    return pd.DataFrame(trades, columns=["entry_time", "dir", "entry", "exit", "net_bps"])


def summary(bps: pd.Series) -> dict:
    x = bps.dropna().to_numpy(float)
    n = len(x)
    if n < 2:
        return {"N": n, "mean_bps": float("nan"), "t": float("nan"), "W": float("nan"),
                "PF": float("nan"), "total_bps": float(x.sum()) if n else 0.0}
    sd = x.std(ddof=1)
    w, lo = x[x > 0], x[x < 0]
    return {"N": n, "mean_bps": x.mean(), "t": x.mean() / (sd / math.sqrt(n)) if sd else 0.0,
            "W": len(w) / n, "PF": w.sum() / -lo.sum() if len(lo) else float("inf"),
            "total_bps": x.sum()}
