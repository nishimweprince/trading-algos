"""FU candle detection — port of fu.txt (Pine v5).

Bullish FU: current.low < prev.low AND current.close > prev.high
Bearish FU: current.high > prev.high AND current.close < prev.low

Optional filters (Pine defaults: both off):
- Doji-leading: prior candle must be a doji (body <= range * dojiBodyRatio)
- SMA: bullish FU requires close > SMA(N), bearish requires close < SMA(N)
"""
from __future__ import annotations

from typing import List

import pandas as pd

from app.core.types import Direction, FUEvent
from app.indicators.common import index_time, is_doji, sma


def detect_fu(df: pd.DataFrame,
              use_doji_filter: bool = False,
              use_ma_filter: bool = False,
              sma_length: int = 9,
              doji_body_ratio: float = 0.3) -> List[FUEvent]:
    """Scan a DataFrame and return all FU events (oldest first)."""
    if len(df) < 2:
        return []

    sma_series = sma(df['close'], sma_length) if use_ma_filter else None

    events: List[FUEvent] = []
    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    idx = df.index

    for i in range(1, len(df)):
        prev_h, prev_l = h[i - 1], l[i - 1]
        ch, cl, cc, co = h[i], l[i], c[i], o[i]

        is_bull = cl < prev_l and cc > prev_h
        is_bear = ch > prev_h and cc < prev_l
        if not (is_bull or is_bear):
            continue

        leading_doji = is_doji(o[i - 1], prev_h, prev_l, c[i - 1], doji_body_ratio)
        if use_doji_filter and not leading_doji:
            continue

        sma_aligned = False
        if use_ma_filter:
            sma_v = sma_series.iloc[i] if sma_series is not None else None
            if sma_v is None or pd.isna(sma_v):
                continue
            if is_bull and not (cc > sma_v):
                continue
            if is_bear and not (cc < sma_v):
                continue
            sma_aligned = True

        events.append(FUEvent(
            time=index_time(df, i),
            direction=Direction.BUY if is_bull else Direction.SELL,
            open=float(co), high=float(ch), low=float(cl), close=float(cc),
            prev_high=float(prev_h),
            prev_low=float(prev_l),
            swept_level=float(prev_l if is_bull else prev_h),
            leading_doji=bool(leading_doji),
            sma_aligned=sma_aligned,
        ))

    return events


def detect_latest_fu(df: pd.DataFrame, **kwargs) -> FUEvent | None:
    """Return only the FU event for the most recent closed candle, if any."""
    if len(df) < 2:
        return None
    events = detect_fu(df, **kwargs)
    if not events:
        return None
    latest_time = index_time(df, len(df) - 1)
    latest = events[-1]
    return latest if latest.time == latest_time else None
