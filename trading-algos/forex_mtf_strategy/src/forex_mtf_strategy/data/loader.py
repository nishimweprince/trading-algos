from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


_TIME_COL_CANDIDATES: tuple[str, ...] = ("time", "datetime", "date", "timestamp")


def load_ohlcv_csv(
    csv_path: str | Path,
    *,
    time_col: str | None = None,
    tz: str = "UTC",
) -> pd.DataFrame:
    """
    Load OHLCV candles from CSV into a DataFrame indexed by datetime.

    Required columns (case-insensitive): open, high, low, close
    Optional: volume
    """
    path = Path(csv_path)
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    if time_col is None:
        for candidate in _TIME_COL_CANDIDATES:
            if candidate in df.columns:
                time_col = candidate
                break
    if time_col is None or time_col not in df.columns:
        raise ValueError(
            f"Could not find a time column. Tried: {list(_TIME_COL_CANDIDATES)}"
        )

    df[time_col] = _parse_time_col(df[time_col], tz=tz)
    df = df.set_index(time_col).sort_index()

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 1.0
    else:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(1.0)

    df = df[["open", "high", "low", "close", "volume"]].dropna()
    df = df[~df.index.duplicated(keep="last")]
    return df


def _parse_time_col(s: Iterable, *, tz: str) -> pd.DatetimeIndex:
    # epoch seconds or ms
    if pd.api.types.is_numeric_dtype(s):
        # heuristics: if values are too large, treat as ms
        series = pd.to_numeric(pd.Series(s), errors="coerce")
        if series.dropna().median() > 10_000_000_000:  # ~2286-11-20 in seconds
            dt = pd.to_datetime(series, unit="ms", utc=True, errors="coerce")
        else:
            dt = pd.to_datetime(series, unit="s", utc=True, errors="coerce")
        return dt.tz_convert(tz)

    dt = pd.to_datetime(pd.Series(s), utc=True, errors="coerce")
    return dt.dt.tz_convert(tz)

