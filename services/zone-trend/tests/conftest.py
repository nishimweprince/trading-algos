from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from zone_trend.data.parse import KLINE_COLUMNS
from zone_trend.data.store import DataPaths, write_month

HOUR = 3_600_000
MINUTE = 60_000
DAY = 86_400_000


def ms(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").timestamp() * 1000)


def klines(start: str | int, n: int, step: int, price: float = 100.0, volume: float = 1.0):
    """n well-formed bars from ``start``: a gentle zig-zag with a constant volume."""
    t0 = ms(start) if isinstance(start, str) else start
    times = t0 + np.arange(n, dtype=np.int64) * step
    close = price + np.sin(np.arange(n) / 7.0)
    open_ = np.concatenate([[price], close[:-1]])
    df = pd.DataFrame(
        {
            "open_time": times,
            "open": open_,
            "high": np.maximum(open_, close) + 0.1,
            "low": np.minimum(open_, close) - 0.1,
            "close": close,
            "volume": np.full(n, volume),
            "close_time": times + step - 1,
            "quote_volume": np.full(n, volume) * close,
            "count": np.full(n, 10, dtype=np.int64),
            "taker_buy_volume": np.full(n, volume / 2),
            "taker_buy_quote_volume": np.full(n, volume / 2) * close,
        }
    )
    df["open_dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def kline_zip(df: pd.DataFrame, header: bool) -> bytes:
    out = df[KLINE_COLUMNS[:-1]].copy()
    out["ignore"] = 0
    csv = out.to_csv(index=False, header=header).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", csv)
    return buf.getvalue()


def put(paths: DataPaths, dataset: str, symbol: str, tf: str, df: pd.DataFrame) -> None:
    """Write a series as month files, the way sync does."""
    month = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.strftime("%Y-%m")
    for m, part in df.groupby(month):
        write_month(paths.month_file(dataset, symbol, tf, m), part, source="monthly", files=1)


@pytest.fixture
def paths(tmp_path) -> DataPaths:
    return DataPaths(tmp_path / "data")
