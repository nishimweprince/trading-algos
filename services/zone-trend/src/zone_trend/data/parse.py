"""Vision zip archives to typed DataFrames.

Kline-shaped datasets (``klines``, ``markPriceKlines``, ``indexPriceKlines``)
share one 12-column layout; some files carry a header row and some do not, so
the header is detected by whether the first field is numeric. Timestamps stay
int64 ms UTC, with a derived ``open_dt`` for convenience.

``fundingRate``'s ``calc_time`` carries a few milliseconds of jitter
(``...000001``); ``settle_time`` is it rounded to the second, which is the
timestamp the backtester charges funding at.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = ["FUNDING_COLUMNS", "KLINE_COLUMNS", "read_archive"]

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "last_funding_rate"]

# Above this a timestamp is in microseconds, not milliseconds (year ~5138 in ms).
_US_THRESHOLD = 100_000_000_000_000


def _csv_bytes(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{path.name}: expected one csv, found {names}")
        return zf.read(names[0])


def _has_header(raw: bytes) -> bool:
    first = raw.split(b",", 1)[0].strip()
    return not first.lstrip(b"-").replace(b".", b"", 1).isdigit()


def _to_ms(series: pd.Series) -> pd.Series:
    values = series.astype("int64")
    if len(values) and values.max() > _US_THRESHOLD:
        values = values // 1000
    return values


def read_klines(raw: bytes) -> pd.DataFrame:
    header = 0 if _has_header(raw) else None
    df = pd.read_csv(io.BytesIO(raw), header=header, names=KLINE_COLUMNS if header is None else None)
    if header is not None:
        df.columns = KLINE_COLUMNS
    df = df.drop(columns="ignore")
    for col in df.columns:
        if col in ("open_time", "close_time"):
            df[col] = _to_ms(df[col])
        elif col == "count":
            df[col] = df[col].astype("int64")
        else:
            df[col] = df[col].astype("float64")
    df["open_dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def read_funding(raw: bytes) -> pd.DataFrame:
    header = 0 if _has_header(raw) else None
    df = pd.read_csv(
        io.BytesIO(raw), header=header, names=FUNDING_COLUMNS if header is None else None
    )
    df.columns = FUNDING_COLUMNS
    df["calc_time"] = _to_ms(df["calc_time"])
    df["settle_time"] = (np.round(df["calc_time"] / 1000) * 1000).astype("int64")
    df["funding_interval_hours"] = df["funding_interval_hours"].astype("int64")
    df["last_funding_rate"] = df["last_funding_rate"].astype("float64")
    df["settle_dt"] = pd.to_datetime(df["settle_time"], unit="ms", utc=True)
    return df


def read_archive(path: Path, dataset: str) -> pd.DataFrame:
    raw = _csv_bytes(path)
    if not raw.strip():
        cols = FUNDING_COLUMNS if dataset == "fundingRate" else KLINE_COLUMNS[:-1]
        return pd.DataFrame(columns=cols)
    return read_funding(raw) if dataset == "fundingRate" else read_klines(raw)
