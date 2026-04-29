"""Shared helpers for indicator modules.

Conventions:
- Input is a pandas DataFrame indexed by datetime with columns
  open, high, low, close, volume.
- Detection functions are pure and return event lists or new state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from app.core.types import ensure_utc


def doji_ratio(o: float, h: float, l: float, c: float) -> float:
    """abs(close-open) / range. Returns 1.0 if range is zero (degenerate)."""
    rng = h - l
    if rng <= 0:
        return 1.0
    return abs(c - o) / rng


def is_doji(o: float, h: float, l: float, c: float, max_body_ratio: float) -> bool:
    """Pine: math.abs(close-open) <= (high-low) * dojiBodyRatio."""
    return abs(c - o) <= (h - l) * max_body_ratio


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=length, min_periods=1).mean()


def index_time(df: pd.DataFrame, i: int) -> datetime:
    ts = df.index[i]
    if hasattr(ts, 'to_pydatetime'):
        ts = ts.to_pydatetime()
    return ensure_utc(ts)


def overlaps(top_a: float, bot_a: float, top_b: float, bot_b: float) -> bool:
    return not (top_a < bot_b or bot_a > top_b)


def in_range(price: float, top: float, bottom: float) -> bool:
    return bottom <= price <= top


def safe_pct(num: float, denom: float) -> Optional[float]:
    if denom == 0:
        return None
    return num / denom
