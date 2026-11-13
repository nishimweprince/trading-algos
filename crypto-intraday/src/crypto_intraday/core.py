"""Load 5m klines and compute the PREREG.md rules."""

from __future__ import annotations

import math
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .fetch import DEFAULT_OUT

UTC, ET = ZoneInfo("UTC"), ZoneInfo("America/New_York")
COLS = ["open_time", "open", "high", "low", "close", "volume"]


def load_5m(symbol: str, root: Path = DEFAULT_OUT) -> pd.DataFrame:
    """All monthly zips for ``symbol`` -> DataFrame indexed by bar-open time (UTC)."""
    files = sorted((root / symbol).glob(f"{symbol}-5m-*.zip"))
    if not files:
        raise FileNotFoundError(f"no {symbol} zips under {root / symbol}; run fetch first")
    parts = []
    for f in files:
        with zipfile.ZipFile(f) as z:
            with z.open(z.namelist()[0]) as fh:
                df = pd.read_csv(fh, header=None, usecols=range(6), names=COLS, dtype=str)
        df = df[df.open_time.str.isdigit()]  # newer files carry a header row
        t = df.open_time.astype("int64")
        unit = np.where(t > 10**14, "us", "ms")
        ts = pd.to_datetime(np.where(unit == "us", t // 1000, t), unit="ms", utc=True)
        parts.append(pd.DataFrame({c: df[c].astype(float).to_numpy() for c in COLS[1:]},
                                  index=ts))
    out = pd.concat(parts)
    return out[~out.index.duplicated()].sort_index()


def _ts(d: date, hh: int, mm: int, tz: ZoneInfo = UTC) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm), tzinfo=tz)).tz_convert("UTC")


def trade(px: pd.Series, a: pd.Timestamp, b: pd.Timestamp, side: int) -> float:
    pa, pb = px.get(a, np.nan), px.get(b, np.nan)
    return side * (pb - pa) / pa * 1e4  # gross bp


def r1(px: pd.Series, d: date) -> float:
    """Long 22:00 UTC on day d -> 00:00 UTC on d+1."""
    return trade(px, _ts(d, 22, 0), _ts(d + timedelta(days=1), 0, 0), 1)


def r2(px: pd.Series, d: date, clock: str = "utc") -> tuple[float, float]:
    """(signal return bp, trade gross bp). UTC clock or US-equity clock (ET, weekdays)."""
    if clock == "utc":
        s0, s1 = _ts(d, 0, 0), _ts(d, 0, 30)
        e0, e1 = _ts(d, 23, 30), _ts(d + timedelta(days=1), 0, 0)
    else:
        s0, s1 = _ts(d, 9, 30, ET), _ts(d, 10, 0, ET)
        e0, e1 = _ts(d, 15, 30, ET), _ts(d, 16, 0, ET)
    sig = trade(px, s0, s1, 1)
    if not np.isfinite(sig) or sig == 0:
        return sig, np.nan
    return sig, trade(px, e0, e1, 1 if sig > 0 else -1)


def day_table(px: pd.Series, start: date, end: date) -> pd.DataFrame:
    rows, d = [], start
    while d <= end:
        s_utc, t_utc = r2(px, d, "utc")
        row = {"date": d, "r1": r1(px, d), "r2_sig": s_utc, "r2": t_utc}
        if d.weekday() < 5:
            s_et, t_et = r2(px, d, "et")
            row.update({"r2et": t_et,
                        "etf_pre": trade(px, _ts(d, 15, 0, ET), _ts(d, 16, 0, ET), 1),
                        "etf_post": trade(px, _ts(d, 16, 0, ET), _ts(d, 17, 0, ET), 1)})
        rows.append(row)
        d += timedelta(days=1)
    return pd.DataFrame(rows).set_index("date")


def hourly_means(px: pd.Series) -> pd.DataFrame:
    """Mean bp return per UTC hour, before vs after the spot-ETF launch (2024-01-11)."""
    h = px[px.index.minute == 0]
    ret = (h.shift(-1) / h - 1) * 1e4
    ret = ret[(h.index + pd.Timedelta(hours=1)).isin(h.index)]  # contiguous hours only
    cut = pd.Timestamp("2024-01-11", tz="UTC")
    out = {}
    for name, m in (("pre_etf", ret.index < cut), ("post_etf", ret.index >= cut)):
        g = ret[m].groupby(ret[m].index.hour)
        out[f"{name}_mean"] = g.mean()
        out[f"{name}_t"] = g.mean() / (g.std() / np.sqrt(g.count()))
    df = pd.DataFrame(out)
    df.index.name = "UTC hour"
    return df


def summarize(x: pd.Series, cost_bp: float = 0.0) -> dict:
    v = (x.dropna() - cost_bp).to_numpy(float)
    n = len(v)
    if n < 2:
        return {"N": n}
    sd = v.std(ddof=1)
    t = v.mean() / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    w, lo = v[v > 0], v[v < 0]
    return {"N": n, "mean_bp": v.mean(), "t": t, "p": 0.5 * math.erfc(t / math.sqrt(2)),
            "W": len(w) / n, "PF": w.sum() / -lo.sum() if len(lo) else float("inf"),
            "total_bp": v.sum()}


def holm(p: dict[str, float], alpha: float = 0.05) -> dict[str, bool]:
    out, ok = {}, True
    for i, k in enumerate(sorted(p, key=p.get)):
        ok = ok and p[k] <= alpha / (len(p) - i)
        out[k] = ok
    return out
