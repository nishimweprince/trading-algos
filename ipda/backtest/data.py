"""Load HistData XAUUSD M1 bid files into one UTC-indexed frame (cached as parquet).

HistData documents its timestamps as fixed EST, but the gold files place the daily
17:00-18:00 maintenance break at the same wall-clock time in July and January — they
follow America/New_York *with* DST. Localising to New York is what makes the Tokyo and
New York session windows line up with the live service.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_DATA_DIR = Path("/Users/pnishimwe/Documents/Markets/Data/XAUUSD")
CACHE_DIR = Path(__file__).resolve().parent / ".cache"


def _read_one(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        sep=";",
        header=None,
        names=["ts", "open", "high", "low", "close", "vol"],
        usecols=[0, 1, 2, 3, 4],
        dtype={"ts": str},
        engine="c",
    )
    return df


def load_minutes(data_dir: Path = DEFAULT_DATA_DIR, refresh: bool = False) -> pd.DataFrame:
    files = sorted(data_dir.glob("DAT_ASCII_XAUUSD_M1_*.csv"))
    if not files:
        raise FileNotFoundError(f"no DAT_ASCII_XAUUSD_M1_*.csv in {data_dir}")
    CACHE_DIR.mkdir(exist_ok=True)
    stamp = f"{len(files)}_{max(f.stat().st_mtime_ns for f in files)}"
    cache = CACHE_DIR / f"xauusd_m1_{stamp}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    raw = pd.concat([_read_one(f) for f in files], ignore_index=True)
    local = pd.to_datetime(raw["ts"], format="%Y%m%d %H%M%S")
    utc = local.dt.tz_localize(
        "America/New_York", ambiguous="NaT", nonexistent="NaT"
    ).dt.tz_convert("UTC")
    df = pd.DataFrame(
        {
            "ts": utc,
            "open": raw["open"].astype(np.float64),
            "high": raw["high"].astype(np.float64),
            "low": raw["low"].astype(np.float64),
            "close": raw["close"].astype(np.float64),
        }
    )
    df = df.dropna(subset=["ts"]).drop_duplicates("ts", keep="last")
    df = df.sort_values("ts").reset_index(drop=True)
    for old in CACHE_DIR.glob("xauusd_m1_*.parquet"):
        old.unlink()
    df.to_parquet(cache)
    return df
