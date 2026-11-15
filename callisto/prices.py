"""Free M1 bid/ask candles from Dukascopy's public datafeed, cached per day.

URL: https://datafeed.dukascopy.com/datafeed/{SYM}/{YYYY}/{MM-1:02}/{DD:02}/{BID|ASK}_candles_min_1.bi5
Each record is 24 bytes, big-endian: seconds-from-midnight-UTC, open, close, low, high (ints
scaled by 10^digits) and a float volume. The feed throttles (429/503), so we retry slowly.
"""
from __future__ import annotations

import lzma
import struct
import time
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import requests
from config import CFG

SCALE = {"XAUUSD": 1000, "XAGUSD": 1000}
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://freeserv.dukascopy.com/"}


def _scale(sym: str) -> int:
    if sym in SCALE:
        return SCALE[sym]
    return 1000 if sym.endswith("JPY") else 100000


def _fetch_day(sym: str, day: date, side: str) -> bytes:
    cache = CFG.data_dir / "prices" / sym / f"{day.isoformat()}_{side}.bi5"
    today = datetime.now(UTC).date()
    # Past days never change; today's file grows, so it is refreshed every 15 minutes.
    if cache.exists() and (day < today or time.time() - cache.stat().st_mtime < 900):
        return cache.read_bytes()
    url = (f"https://datafeed.dukascopy.com/datafeed/{sym}/{day.year}/{day.month - 1:02d}/"
           f"{day.day:02d}/{side}_candles_min_1.bi5")
    status = None
    for attempt in range(10):
        try:
            r = requests.get(url, headers=_UA, timeout=30)
        except requests.RequestException as e:
            status = repr(e)
            time.sleep(2 + 3 * attempt)
            continue
        status = r.status_code
        if r.status_code == 200:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
            return r.content
        if r.status_code == 404:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(b"")
            return b""
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"dukascopy {url}: {status}")


def _decode(raw: bytes, day: date, scale: int) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=["o", "h", "l", "c"])
    b = lzma.decompress(raw)
    n = len(b) // 24
    recs = struct.unpack(">" + "5if" * n, b[: n * 24])
    a = np.array(recs, dtype=float).reshape(n, 6)
    base = pd.Timestamp(day)
    idx = base + pd.to_timedelta(a[:, 0], unit="s")
    df = pd.DataFrame({"o": a[:, 1], "c": a[:, 2], "l": a[:, 3], "h": a[:, 4], "v": a[:, 5]},
                      index=idx)
    # Dukascopy fills closed-market minutes with flat zero-volume candles; drop them.
    df = df[df["v"] > 0]
    return df[["o", "h", "l", "c"]] / scale


def load(sym: str, start: datetime, end: datetime) -> pd.DataFrame:
    """M1 bars, UTC naive index, columns bo/bh/bl/bc (bid) and ao/ah/al/ac (ask)."""
    sym = sym.upper()
    today = datetime.now(UTC).date()
    frames = []
    d = start.date()
    while d <= min(end.date(), today):
        if d.weekday() != 5:  # Saturday: market closed, no file worth fetching
            bid = _decode(_fetch_day(sym, d, "BID"), d, _scale(sym)).add_prefix("b")
            ask = _decode(_fetch_day(sym, d, "ASK"), d, _scale(sym)).add_prefix("a")
            frames.append(bid.join(ask, how="inner"))
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index()
