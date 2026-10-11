"""Coarser kline months derived from 1m months already on disk.

The sub-hourly entry prototypes do not download native 15m/5m archives: 1m
bars aggregate to 15m and 5m bars exactly (the validator checks 1m -> 1h the
same way), so ``resample_symbol`` derives them locally with
``source="resample-1m"`` in the month metadata. Only complete spans (all
minutes present) become bars; a bar belongs to its open time's calendar
month, so files hold whole bars of their own month.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from zone_trend.data.store import DataPaths, write_month

__all__ = ["FIVE_MS", "QUARTER_MS", "resample_5m", "resample_15m", "resample_symbol"]

FIVE_MS = 5 * 60_000
QUARTER_MS = 15 * 60_000
_SPANS = {"5m": (FIVE_MS, 5), "15m": (QUARTER_MS, 15)}

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


def _resample(m1: pd.DataFrame, span_ms: int, minutes: int) -> pd.DataFrame:
    """Aggregate 1m klines to closed bars; incomplete spans are dropped."""
    if m1.empty:
        return pd.DataFrame(columns=_COLUMNS)
    key = m1["open_time"] // span_ms * span_ms
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
    out = out[out["n"] == minutes].drop(columns="n")
    out["open_time"] = out.index.astype("int64")
    out["close_time"] = out["open_time"] + span_ms - 1
    return out[_COLUMNS].reset_index(drop=True)


def resample_15m(m1: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m klines to closed 15m bars; incomplete quarters are dropped."""
    return _resample(m1, QUARTER_MS, 15)


def resample_5m(m1: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m klines to closed 5m bars; incomplete spans are dropped."""
    return _resample(m1, FIVE_MS, 5)


def _month(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m")


def resample_symbol(
    paths: DataPaths, symbol: str, force: bool = False, tf: str = "15m"
) -> tuple[int, int]:
    """Build every missing ``tf`` month for ``symbol`` from its 1m months.

    Returns ``(built, skipped)``. Months are independent: a span can never
    straddle a month boundary (5m and 15m spans align to the hour), so each
    1m month file converts to its own month file. Re-run with ``force`` to
    refresh the still-growing current month.
    """
    span_ms, minutes = _SPANS[tf]
    built, skipped = 0, 0
    for src in paths.months("klines", symbol, "1m"):
        target = paths.month_file("klines", symbol, tf, src.stem)
        if target.exists() and not force:
            skipped += 1
            continue
        m1 = pq.read_table(src).to_pandas()
        bars = _resample(m1, span_ms, minutes)
        bars = bars[bars["open_time"].map(_month) == src.stem]
        write_month(target, bars.reset_index(drop=True), source="resample-1m", files=1)
        built += 1
    return built, skipped


def resample_main(
    data_dir: Path | None, symbols: list[str], force: bool, tf: str = "15m"
) -> dict[str, tuple[int, int]]:
    paths = DataPaths.from_env(data_dir)
    return {s: resample_symbol(paths, s, force, tf) for s in symbols}
