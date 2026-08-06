"""Canonical UTC H4 candles derived exclusively from closed H1 candles."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services.candle_quality import CANDLE_COLUMNS, unexpected_gaps, validate_candles
from app.utils.parquet import month_partition_path, write_month_partition


def derive_h4(frame: pd.DataFrame, *, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Aggregate H1 close-stamped bars into fixed UTC four-hour buckets."""
    validate_candles(frame)
    h1 = frame[CANDLE_COLUMNS].copy().sort_values("ts")
    h1["ts"] = pd.to_datetime(h1["ts"], utc=True)
    # Subtracting a nanosecond makes 04:00 belong to the bucket ending 04:00.
    h1["bucket_end"] = (h1["ts"] - pd.Timedelta(nanoseconds=1)).dt.floor("4h") + pd.Timedelta(hours=4)
    cutoff = pd.Timestamp(as_of) if as_of is not None else h1["ts"].max()
    cutoff = cutoff.tz_convert("UTC") if cutoff.tzinfo else cutoff.tz_localize("UTC")
    rows = []
    for bucket_end, group in h1.groupby("bucket_end", sort=True):
        group = group.sort_values("ts")
        expected_first = bucket_end - pd.Timedelta(hours=3)
        if (
            bucket_end > cutoff
            or group.iloc[0]["ts"] != expected_first
            or group.iloc[-1]["ts"] != bucket_end
            or unexpected_gaps(group)
        ):
            continue
        rows.append(
            {
                "ts": bucket_end,
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": float(group["volume"].sum()),
            }
        )
    return pd.DataFrame(rows, columns=CANDLE_COLUMNS)


def rebuild_h4(candle_root: Path, symbol: str = "XAUUSD") -> int:
    base = candle_root / f"symbol={symbol.upper()}" / "timeframe=H1"
    files = sorted(base.glob("year=*/month=*/part-*.parquet"))
    if not files:
        raise ValueError(f"No H1 candles found for {symbol}")
    h1 = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    h4 = derive_h4(h1)
    derived_root = candle_root / f"symbol={symbol.upper()}" / "timeframe=H4"
    for existing in derived_root.glob("year=*/month=*/part-*.parquet"):
        existing.unlink()
    if h4.empty:
        return 0
    h4["year"] = h4["ts"].dt.year
    h4["month"] = h4["ts"].dt.month
    for (year, month), group in h4.groupby(["year", "month"]):
        path = month_partition_path(candle_root, symbol.upper(), "H4", int(year), int(month))
        write_month_partition(path, group, CANDLE_COLUMNS)
    return len(h4)
