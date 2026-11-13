"""Load HistData.com ASCII M1 bars for USD/JPY.

File rows look like ``20200101 170000;108.730000;108.730000;108.728000;108.728000;0``
(timestamp;open;high;low;close;volume). HistData documents the stamps as EST
without DST, but the files in Data/USDJPY show the Sunday open at 17:00 and the
08:30 US payrolls bar at 08:30 in both summer and winter, so they are New York
wall-clock time WITH DST. We localise to America/New_York. The bars are bid
prices; spread is charged as a cost per trade in the backtest.

The minute grid this module returns is in JST, with one *price* per minute:
``price_at(t)`` is the close of the bar that ends at ``t`` (the bar stamped
``t - 1min``), forward-filled at most ``MAX_STALE_MIN`` minutes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

DEFAULT_DATA_DIR = Path(
    os.environ.get("GOTOBI_DATA_DIR", "/Users/pnishimwe/Documents/Markets/Data/USDJPY")
)
SOURCE_TZ = "America/New_York"
JST = "Asia/Tokyo"
MAX_STALE_MIN = 5


def _read_one(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        sep=";",
        header=None,
        names=["ts", "open", "high", "low", "close", "volume"],
        dtype={"ts": str},
    )
    ts = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S")
    # The repeated 01:00-02:00 hour in November and the skipped hour in March fall
    # around 14:00-16:00 JST, outside every window we trade, so dropping them is safe.
    ts = ts.dt.tz_localize(SOURCE_TZ, ambiguous="NaT", nonexistent="NaT")
    df = df.assign(ts=ts).dropna(subset=["ts"]).set_index("ts")
    return df[["open", "high", "low", "close"]]


def load_m1(data_dir: Path | str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """All USDJPY M1 bars in ``data_dir``, indexed by JST bar-open time."""
    files = sorted(Path(data_dir).glob("DAT_ASCII_USDJPY_M1_*.csv"))
    if not files:
        raise FileNotFoundError(f"no DAT_ASCII_USDJPY_M1_*.csv files in {data_dir}")
    df = pd.concat([_read_one(f) for f in files])
    df.index = df.index.tz_convert(JST)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def price_grid(bars: pd.DataFrame) -> pd.Series:
    """Price at each JST minute boundary: close of the bar ending there, ffilled <= 5 min."""
    close = bars["close"].copy()
    close.index = close.index + pd.Timedelta(minutes=1)  # bar [t-1m, t) -> price at t
    full = pd.date_range(close.index[0], close.index[-1], freq="1min", tz=JST)
    return close.reindex(full).ffill(limit=MAX_STALE_MIN)
