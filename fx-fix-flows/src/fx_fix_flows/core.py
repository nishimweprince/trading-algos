"""Data, window clock times and per-day leg returns for PREREG.md."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

DATA_ROOT = Path("/Users/pnishimwe/Documents/Markets/Data")
ET, TOKYO = ZoneInfo("America/New_York"), ZoneInfo("Asia/Tokyo")
BERLIN, LONDON, SYDNEY = ZoneInfo("Europe/Berlin"), ZoneInfo("Europe/London"), ZoneInfo(
    "Australia/Sydney")
PIP = {"USDJPY": 0.01, "AUDUSD": 0.0001}
# +1 if the pair rises when the USD appreciates.
USD_SIGN = {"USDJPY": 1, "AUDUSD": -1}
# Local currency of the pair and its BR session (start, end, tz).
LOCAL_SESSION = {"USDJPY": ((8, 0), (15, 0), TOKYO), "AUDUSD": ((10, 0), (16, 0), SYDNEY)}
US_SESSION = ((8, 0), (16, 0), ET)
MAX_STALE_MIN = 5


def load_m1(symbol: str, root: Path = DATA_ROOT) -> pd.DataFrame:
    files = sorted((root / symbol).glob(f"DAT_ASCII_{symbol}_M1_*.csv"))
    files = [f for f in files if int(f.stem.split("_")[-1][:4]) >= 2020]
    if not files:
        raise FileNotFoundError(f"no {symbol} M1 files under {root / symbol}")
    parts = []
    for f in files:
        df = pd.read_csv(f, sep=";", header=None, dtype={0: str},
                         names=["ts", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S").dt.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="NaT")
        parts.append(df.assign(ts=ts).dropna(subset=["ts"]).set_index("ts")[["close"]])
    out = pd.concat(parts)
    out.index = out.index.tz_convert("UTC")
    return out[~out.index.duplicated()].sort_index()


def price_grid(m1: pd.DataFrame) -> pd.Series:
    """Price at each UTC minute boundary = close of the bar ending there, ffilled <= 5 min."""
    c = m1["close"].copy()
    c.index = c.index + pd.Timedelta(minutes=1)
    full = pd.date_range(c.index[0], c.index[-1], freq="1min", tz="UTC")
    return c.reindex(full).ffill(limit=MAX_STALE_MIN)


def _at(d: date, hm: tuple[int, int], tz: ZoneInfo) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(*hm), tzinfo=tz)).tz_convert("UTC")


def legs_for_day(d: date, symbol: str) -> dict[str, tuple[pd.Timestamp, pd.Timestamp, int]]:
    """leg -> (start UTC, end UTC, position in the pair: +1 long / -1 short)."""
    u = USD_SIGN[symbol]
    open_ = _at(d - timedelta(days=1), (17, 0), ET)
    tokyo = _at(d, (9, 55), TOKYO)
    eu_open = _at(d, (2, 0), ET)
    ecb = _at(d, (14, 15), BERLIN)
    ldn = _at(d, (16, 0), LONDON)
    close = _at(d, (17, 0), ET)
    s0, s1, stz = LOCAL_SESSION[symbol]
    loc_pos = 1 if symbol == "USDJPY" else -1  # short the local currency in its own hours
    return {
        "preT": (open_, tokyo, +u),
        "postT": (tokyo, eu_open, -u),
        "preE": (eu_open, ecb, +u),
        "postL": (ldn, close, -u),
        "br_local": (_at(d, s0, stz), _at(d, s1, stz), loc_pos),
        "br_us": (_at(d, US_SESSION[0], ET), _at(d, US_SESSION[1], ET), -loc_pos),
    }


STRATS = {"T": ("preT", "postT"), "E": ("preE", "postL"),
          "W": ("preT", "postT", "preE", "postL"), "BR": ("br_local", "br_us")}


def day_table(grid: pd.Series, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Gross bp per leg per FX day (Mon-Fri), NaN where a price is missing."""
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            row = {"date": d}
            for leg, (a, b, pos) in legs_for_day(d, symbol).items():
                pa, pb = grid.get(a, np.nan), grid.get(b, np.nan)
                row[leg] = pos * (pb - pa) / pa * 1e4
                row[f"{leg}_px"] = pa
            rows.append(row)
        d += timedelta(days=1)
    return pd.DataFrame(rows).set_index("date")


def strategy_net(tbl: pd.DataFrame, symbol: str, strat: str, cost_pips: float) -> pd.Series:
    """Daily net bp: sum of legs minus one round-trip cost per leg; NaN if any leg missing."""
    legs = STRATS[strat]
    gross = tbl[list(legs)].sum(axis=1, min_count=len(legs))
    cost = sum(cost_pips * PIP[symbol] / tbl[f"{leg}_px"] * 1e4 for leg in legs)
    return gross - cost


def summarize(x: pd.Series) -> dict:
    v = x.dropna().to_numpy(float)
    n = len(v)
    if n < 2:
        return {"N": n}
    sd = v.std(ddof=1)
    t = v.mean() / (sd / math.sqrt(n))
    w, lo = v[v > 0], v[v < 0]
    return {"N": n, "mean_bp": v.mean(), "t": t, "p": 0.5 * math.erfc(t / math.sqrt(2)),
            "W": len(w) / n, "PF": w.sum() / -lo.sum() if len(lo) else float("inf"),
            "ann_bp": v.mean() * 252, "sharpe": v.mean() / sd * math.sqrt(252)}


def holm(p: dict[str, float], alpha: float = 0.05) -> dict[str, bool]:
    out, ok = {}, True
    for i, k in enumerate(sorted(p, key=p.get)):
        ok = ok and p[k] <= alpha / (len(p) - i)
        out[k] = ok
    return out
