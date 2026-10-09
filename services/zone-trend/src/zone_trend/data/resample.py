"""Coarser kline months derived from 1m months already on disk.

The 15m entry prototype does not download native 15m archives: 1m bars
aggregate to 15m bars exactly (the validator checks 1m -> 1h the same way),
so ``resample_symbol`` derives them locally with ``source="resample-1m"`` in
the month metadata. Only complete quarters (all 15 minutes present) become
bars; a 15m bar belongs to its open time's calendar month, so files hold
whole bars of their own month.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from zone_trend.data.store import DataPaths, write_month

__all__ = ["QUARTER_MS", "resample_15m", "resample_symbol"]

QUARTER_MS = 15 * 60_000

_COLUMNS = [
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
]


def resample_15m(m1: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m klines to closed 15m bars; incomplete quarters are dropped."""
    if m1.empty:
        return pd.DataFrame(columns=_COLUMNS)
    key = m1["open_time"] // QUARTER_MS * QUARTER_MS
    g = m1.groupby(key)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "quote_volume": g["quote_volume"].sum(),
            "count": g["count"].sum(),
            "taker_buy_volume": g["taker_buy_volume"].sum(),
            "taker_buy_quote_volume": g["taker_buy_quote_volume"].sum(),
            "n": g.size(),
        }
    )
    out = out[out["n"] == 15].drop(columns="n")
    out["open_time"] = out.index.astype("int64")
    out["close_time"] = out["open_time"] + QUARTER_MS - 1
    return out[_COLUMNS].reset_index(drop=True)


def _month(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m")


def resample_symbol(paths: DataPaths, symbol: str, force: bool = False) -> tuple[int, int]:
    """Build every missing 15m month for ``symbol`` from its 1m months.

    Returns ``(built, skipped)``. Months are independent: a quarter can never
    straddle a month boundary (quarters align to the hour), so each 1m month
    file converts to its own 15m month file. Re-run with ``force`` to refresh
    the still-growing current month.
    """
    built, skipped = 0, 0
    for src in paths.months("klines", symbol, "1m"):
        target = paths.month_file("klines", symbol, "15m", src.stem)
        if target.exists() and not force:
            skipped += 1
            continue
        m1 = pq.read_table(src).to_pandas()
        bars = resample_15m(m1)
        bars = bars[bars["open_time"].map(_month) == src.stem]
        write_month(target, bars.reset_index(drop=True), source="resample-1m", files=1)
        built += 1
    return built, skipped


def resample_main(
    data_dir: Path | None, symbols: list[str], force: bool
) -> dict[str, tuple[int, int]]:
    paths = DataPaths.from_env(data_dir)
    return {s: resample_symbol(paths, s, force) for s in symbols}
