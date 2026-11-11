"""Epoch-unit detection and hourly UTC grid helpers."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)


def epoch_unit(values: pd.Series | np.ndarray) -> str:
    """Detect s / ms / us by magnitude (per value median)."""
    v = np.abs(np.asarray(values, dtype="float64"))
    v = v[np.isfinite(v)]
    if v.size == 0:
        return "ms"
    med = float(np.median(v))
    if med > 1e17:
        return "ns"
    if med > 1e14:
        return "us"
    if med > 1e11:
        return "ms"
    return "s"


def to_utc(values: pd.Series) -> pd.Series:
    """Convert integer epochs to tz-aware UTC timestamps, detecting the unit per row.

    Binance spot archives switched from milliseconds to microseconds, so a single
    concatenated series may mix both.
    """
    v = pd.to_numeric(values, errors="coerce").astype("float64")
    out = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, UTC]")
    bands: tuple[tuple[Literal["s", "ms", "us"], float, float], ...] = (
        ("s", 0, 1e11),
        ("ms", 1e11, 1e14),
        ("us", 1e14, 1e17),
    )
    for unit, lo, hi in bands:
        mask = (v.abs() >= lo) & (v.abs() < hi)
        if mask.any():
            out[mask] = pd.to_datetime(v[mask].astype("int64"), unit=unit, utc=True)
    return out


def hour_floor(ts: pd.Series | pd.DatetimeIndex) -> pd.Series | pd.DatetimeIndex:
    return ts.dt.floor("h") if isinstance(ts, pd.Series) else ts.floor("h")


def hourly_grid(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(start.floor("h"), end.floor("h"), freq="h", tz="UTC", name="ts")


def regrid(df: pd.DataFrame) -> pd.DataFrame:
    """Reindex a ts-indexed frame onto a complete hourly grid (missing hours -> NaN)."""
    if df.empty:
        return df
    grid = hourly_grid(df.index.min(), df.index.max())
    return df.reindex(grid)


def missing_hours(s: pd.Series) -> tuple[int, int]:
    """(count of missing hours, longest consecutive gap in hours)."""
    isna = s.isna().to_numpy()
    if not isna.any():
        return 0, 0
    longest = run = 0
    for x in isna:
        run = run + 1 if x else 0
        longest = max(longest, run)
    return int(isna.sum()), int(longest)
