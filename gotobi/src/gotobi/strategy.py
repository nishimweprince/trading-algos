"""Per-day trade outcomes for the paper's strategies, in gross pips (1 pip = 0.01 JPY).

All times are JST and refer to the price grid from ``data.price_grid``:
``p(t)`` is the last bid price at minute boundary ``t``.

H1 plain   long at p(03:00), exit at p(09:55).
H1 GC(n)   long if SMA25 crosses above SMA100 (1-minute prices) during
           [(n-1):30, n:00); entry one minute after the cross is seen
           (no same-bar fill), exit at p(09:55). The paper picks n = 3.
H2         if p(09:00) > p(03:00) ("the anomaly occurred"), short at
           p(09:55), exit at p(12:00).
Combined   H1 GC(3) + H2 on the same day (paper Figure 9).

Costs are not applied here; ``stats`` subtracts a round-trip cost per trade.
"""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np
import pandas as pd

from .data import JST

PIP = 0.01
SMA_SHORT = 25
SMA_LONG = 100
GC_WINDOWS = (1, 2, 3, 4, 5)
GC_PAPER_N = 3


def _ts(d: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=JST)


def golden_crosses(grid: pd.Series) -> pd.Series:
    """Boolean series: SMA25 crossed above SMA100 at this minute."""
    s = grid.rolling(SMA_SHORT).mean()
    lg = grid.rolling(SMA_LONG).mean()
    above = s > lg
    prev_above = above.shift(1)
    valid = s.notna() & lg.notna() & s.shift(1).notna() & lg.shift(1).notna()
    return (above & (prev_above == False) & valid).astype(bool)  # noqa: E712


def day_table(grid: pd.Series, days: dict[date, bool]) -> pd.DataFrame:
    """One row per tradable day with prices and gross pip outcomes."""
    gc = golden_crosses(grid)
    gc_times = gc.index[gc.to_numpy()]
    rows = []
    for d, is_gotobi in sorted(days.items()):
        p = {k: grid.get(_ts(d, *hm), np.nan) for k, hm in {
            "p0300": (3, 0), "p0900": (9, 0), "p0955": (9, 55),
            "p0956": (9, 56), "p1200": (12, 0),
        }.items()}
        row = {"date": d, "gotobi": is_gotobi, **p}
        row["h1_plain"] = (p["p0955"] - p["p0300"]) / PIP
        row["anomaly"] = bool(p["p0900"] > p["p0300"]) if not (
            np.isnan(p["p0900"]) or np.isnan(p["p0300"])) else np.nan
        row["h2"] = ((p["p0955"] - p["p1200"]) / PIP) if row["anomaly"] is True else np.nan
        row["post_fix_1m"] = (p["p0956"] - p["p0955"]) / PIP
        for n in GC_WINDOWS:
            lo, hi = _ts(d, n - 1, 30), _ts(d, n, 0)
            i0 = gc_times.searchsorted(lo)
            hit = gc_times[i0] if i0 < len(gc_times) and gc_times[i0] < hi else None
            if hit is None:
                row[f"h1_gc{n}"] = np.nan
                continue
            entry = grid.get(hit + pd.Timedelta(minutes=1), np.nan)
            row[f"h1_gc{n}"] = (p["p0955"] - entry) / PIP
        h1g = row[f"h1_gc{GC_PAPER_N}"]
        legs = [x for x in (h1g, row["h2"]) if not np.isnan(x)]
        row["combined"] = sum(legs) if legs else np.nan
        row["combined_legs"] = len(legs)
        rows.append(row)
    return pd.DataFrame(rows).set_index("date")


def event_paths(grid: pd.Series, days: dict[date, bool],
                start=(0, 0), end=(12, 0)) -> pd.DataFrame:
    """Price minus p(09:55), in pips, per minute of the JST morning; one column per day."""
    cols = {}
    offsets = pd.timedelta_range(
        pd.Timedelta(hours=start[0], minutes=start[1]),
        pd.Timedelta(hours=end[0], minutes=end[1]), freq="1min")
    for d in sorted(days):
        base = grid.get(_ts(d, 9, 55), np.nan)
        if np.isnan(base):
            continue
        idx = pd.Timestamp(datetime.combine(d, time(0, 0)), tz=JST) + offsets
        cols[d] = (grid.reindex(idx).to_numpy() - base) / PIP
    return pd.DataFrame(cols, index=[f"{o.components.hours:02d}:{o.components.minutes:02d}"
                                     for o in offsets])
