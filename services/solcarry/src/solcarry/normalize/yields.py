"""APY -> continuous annual rate, daily -> hourly spreading with bounded forward fill."""

from __future__ import annotations

import numpy as np
import pandas as pd


def apy_to_continuous(apy_pct: pd.Series) -> pd.Series:
    out: pd.Series = np.log1p(pd.to_numeric(apy_pct, errors="coerce") / 100.0)
    return out


def daily_to_hourly(
    daily: pd.Series, max_ffill_days: float, end: pd.Timestamp | None = None
) -> pd.Series:
    """Place each daily value at its day's 00:00 UTC and forward fill hourly.

    Forward fill never bridges a gap longer than ``max_ffill_days``: hours more than
    that far from the last observation stay NaN.
    """
    s = daily.dropna()
    if s.empty:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    idx = pd.DatetimeIndex(s.index)
    idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    s = pd.Series(s.to_numpy(), index=idx.floor("D"))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    last = (end if end is not None else s.index.max() + pd.Timedelta(days=1)).floor("h")
    grid = pd.date_range(s.index.min(), last, freq="h", tz="UTC", name="ts")
    obs_time = pd.Series(s.index, index=s.index).reindex(grid).ffill()
    vals = s.reindex(grid).ffill()
    age = grid.to_series() - obs_time
    limit = pd.Timedelta(days=max_ffill_days)
    vals[age.to_numpy() >= limit] = np.nan
    vals.index.name = "ts"
    return vals
