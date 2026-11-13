"""Panel loading, monthly universe, Donchian-ensemble positions and portfolio returns."""

from __future__ import annotations

import math
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .fetch import DEFAULT_OUT

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
TOP_N = 20
MIN_HISTORY_DAYS = 365
MIN_MEDIAN_QVOL = 2_000_000.0
VOL_TARGET, LEV_CAP, VOL_WINDOW = 0.25, 2.0, 90


def _read_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as fh:
        df = pd.read_csv(fh, header=None, usecols=[0, 4, 7], names=["t", "close", "qvol"],
                         dtype=str)
    df = df[df.t.str.isdigit()]
    t = df.t.astype("int64")
    t = np.where(t > 10**14, t // 1000, t)  # 2025+ archives use microseconds
    idx = pd.to_datetime(t, unit="ms").normalize()
    return pd.DataFrame({"close": df.close.astype(float).to_numpy(),
                         "qvol": df.qvol.astype(float).to_numpy()}, index=idx)


def load_panel(root: Path = DEFAULT_OUT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(close, quote volume) panels: rows = UTC dates, columns = symbols."""
    closes, qvols = {}, {}
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        parts = [_read_zip(f) for f in sorted(d.glob("*.zip"))]
        if not parts:
            continue
        df = pd.concat(parts)
        df = df[~df.index.duplicated()].sort_index()
        closes[d.name], qvols[d.name] = df.close, df.qvol
    close, qvol = pd.DataFrame(closes).sort_index(), pd.DataFrame(qvols).sort_index()
    full = pd.date_range(close.index[0], close.index[-1], freq="D")
    return close.reindex(full), qvol.reindex(full)


def monthly_universe(close: pd.DataFrame, qvol: pd.DataFrame, top_n: int = TOP_N
                     ) -> pd.DataFrame:
    """Boolean panel: True where the coin is a member for that day (refreshed each month)."""
    first = close.apply(pd.Series.first_valid_index)
    med = qvol.rolling(30, min_periods=20).median().shift(1)  # previous 30 days
    member = pd.DataFrame(False, index=close.index, columns=close.columns)
    starts = close.index[close.index.day == 1]
    for i, m in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else close.index[-1] + pd.Timedelta(days=1)
        hist_ok = (m - first).dt.days >= MIN_HISTORY_DAYS
        alive = close.loc[m].notna()
        v = med.loc[m].where(hist_ok & alive & (med.loc[m] >= MIN_MEDIAN_QVOL)).dropna()
        chosen = v.sort_values(ascending=False).index[:top_n]
        member.loc[m:end - pd.Timedelta(days=1), chosen] = True
    return member


def slot_states(c: np.ndarray, hi: np.ndarray, lo: np.ndarray, active: np.ndarray
                ) -> np.ndarray:
    """1/0 long state per day for one (coin, lookback) slot (PREREG signal rules)."""
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    long_, stop = False, -np.inf
    for t in range(n):
        if not active[t] or not np.isfinite(c[t]):
            long_, stop = False, -np.inf
            continue
        mid = (hi[t] + lo[t]) / 2 if np.isfinite(hi[t]) and np.isfinite(lo[t]) else np.nan
        if long_:
            if c[t] < stop:
                long_, stop = False, -np.inf
            elif np.isfinite(mid):
                stop = max(stop, mid)
        elif np.isfinite(hi[t]) and c[t] > hi[t]:
            long_, stop = True, mid if np.isfinite(mid) else -np.inf
        out[t] = 1 if long_ else 0
    return out


def weights(close: pd.DataFrame, member: pd.DataFrame, n_slots_coins: int = TOP_N
            ) -> pd.DataFrame:
    """Coin weights at each close: sum over open slots of min(0.25/sigma90, 2) / (N x 9)."""
    cols = member.columns[member.any()]
    w = pd.DataFrame(0.0, index=close.index, columns=cols)
    slot_cap = 1.0 / (n_slots_coins * len(LOOKBACKS))
    for sym in cols:
        c = close[sym]
        sigma = c.pct_change().rolling(VOL_WINDOW).std() * math.sqrt(365)
        scale = np.minimum(VOL_TARGET / sigma, LEV_CAP).fillna(0.0).to_numpy()
        act = member[sym].to_numpy()
        open_slots = np.zeros(len(c))
        for L in LOOKBACKS:
            hi = c.shift(1).rolling(L).max().to_numpy()
            lo = c.shift(1).rolling(L).min().to_numpy()
            open_slots += slot_states(c.to_numpy(), hi, lo, act)
        w[sym] = open_slots * slot_cap * scale
    return w


def portfolio(close: pd.DataFrame, w: pd.DataFrame, cost: float) -> pd.DataFrame:
    """Daily returns: weights at close t earn t -> t+1; |dw| pays ``cost`` per unit traded."""
    ret = close[w.columns].pct_change().shift(-1)  # return earned after holding at close t
    gross = (w * ret.fillna(0.0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    net = gross - cost * turnover
    out = pd.DataFrame({"gross": gross, "net": net, "turnover": turnover,
                        "exposure": w.sum(axis=1)})
    out.index = out.index + pd.Timedelta(days=1)  # label each return by the day it is earned
    return out


def stats(r: pd.Series, btc: pd.Series) -> dict:
    r = r.dropna()
    b = btc.reindex(r.index).fillna(0.0)
    n = len(r)
    if n < 30:
        return {"days": n}
    eq = (1 + r).cumprod()
    sd = r.std(ddof=1)
    x = np.column_stack([np.ones(n), b.to_numpy()])
    beta, *_ = np.linalg.lstsq(x, r.to_numpy(), rcond=None)
    resid = r.to_numpy() - x @ beta
    se = np.sqrt(np.diag(resid.var(ddof=2) * np.linalg.inv(x.T @ x)))
    return {
        "days": n,
        "CAGR": eq.iloc[-1] ** (365 / n) - 1,
        "vol": sd * math.sqrt(365),
        "sharpe": r.mean() / sd * math.sqrt(365) if sd > 0 else float("nan"),
        "maxDD": float((eq / eq.cummax() - 1).min()),
        "alpha_ann": beta[0] * 365,
        "alpha_t": beta[0] / se[0],
        "beta": beta[1],
    }
