"""Funding normalization. Sign convention everywhere: positive = the SHORT receives."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .time import HOUR, to_utc

log = logging.getLogger(__name__)
HOURS_PER_YEAR = 8760


def velocity_hourly(raw: pd.DataFrame) -> pd.DataFrame:
    """Velocity records -> hourly ``f_h`` (fraction), ``raw_rate`` (USD/SOL), ``oracle_twap``.

    ``fundingRate`` is USD per SOL per period (inherited Drift units), so the
    fractional rate is ``fundingRate / oraclePriceTwap``. Positive = longs pay shorts,
    i.e. the short receives, so no sign flip is needed.
    """
    if raw.empty:
        return pd.DataFrame(
            columns=["f_h", "raw_rate", "oracle_twap", "n_records"],
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        )
    df = raw.copy()
    df["ts_dt"] = pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)
    df["hour"] = df["ts_dt"].dt.floor("h")
    df["frac"] = df["fundingRate"] / df["oraclePriceTwap"]
    g = df.groupby("hour")
    out = pd.DataFrame(
        {
            "f_h": g["frac"].sum(),
            "raw_rate": g["fundingRate"].sum(),
            "oracle_twap": g["oraclePriceTwap"].mean(),
            "n_records": g.size(),
        }
    )
    dups = int((out["n_records"] > 1).sum())
    if dups:
        log.warning("velocity: %d hour buckets with >1 record (summed as separate payments)", dups)
    out.index.name = "ts"
    return out


def binance_hourly(raw: pd.DataFrame) -> pd.DataFrame:
    """Spread each funding payment evenly over the hours *preceding* ``calc_time``.

    ``f_h = last_funding_rate / funding_interval_hours`` for each of those hours.
    """
    cols = ["f_h", "interval_h"]
    if raw.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = raw.copy()
    df["calc"] = to_utc(df["calc_time"]).dt.round("h")
    df = df.dropna(subset=["calc"]).drop_duplicates(subset=["calc"]).sort_values("calc")
    n = pd.to_numeric(df["funding_interval_hours"], errors="coerce").fillna(8).astype(int)
    rate = pd.to_numeric(df["last_funding_rate"], errors="coerce")
    reps = n.to_numpy()
    calc = np.repeat(df["calc"].to_numpy(), reps)
    offs = np.concatenate([np.arange(k, 0, -1) for k in reps]) if len(reps) else np.array([])
    hours = pd.DatetimeIndex(calc) - pd.to_timedelta(offs, unit="h")
    f = np.repeat((rate / n).to_numpy(), reps)
    iv = np.repeat(reps, reps)
    ix = pd.DatetimeIndex(hours, name="ts")
    if ix.tz is None:
        ix = ix.tz_localize("UTC")
    out = pd.DataFrame({"f_h": f, "interval_h": iv}, index=ix)
    # Overlaps can only happen if the interval changes mid-stream; keep the later payment.
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index.name = "ts"
    return out


def hyperliquid_hourly(raw: pd.DataFrame, settle_hours: int = 24) -> pd.DataFrame:
    """Hyperliquid fundingHistory -> hourly ``f_h`` (fraction per hour, short receives +).

    The first weeks of history (May 2023) are 8-hourly snapshots whose period is
    ambiguous; everything before the first run of ``settle_hours`` consecutive hourly
    prints is dropped so only the regular hourly regime is used.
    """
    cols = ["f_h", "premium", "n_records"]
    if raw.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = raw.copy()
    df["hour"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True).dt.floor("h")
    df = df.sort_values("hour").reset_index(drop=True)
    hourly_step = df["hour"].diff().eq(pd.Timedelta(hours=1)).to_numpy()
    start = 0
    run = 0
    for i, ok in enumerate(hourly_step):
        run = run + 1 if ok else 0
        if run >= settle_hours:
            start = i - settle_hours
            break
    df = df.iloc[start:]
    g = df.groupby("hour")
    out = pd.DataFrame(
        {
            "f_h": g["fundingRate"].sum(),
            "premium": g["premium"].mean(),
            "n_records": g.size(),
        }
    )
    out.index.name = "ts"
    return out


def annualize(f_h: pd.Series) -> pd.Series:
    return f_h * HOURS_PER_YEAR


__all__ = [
    "HOUR",
    "HOURS_PER_YEAR",
    "annualize",
    "binance_hourly",
    "hyperliquid_hourly",
    "velocity_hourly",
]
