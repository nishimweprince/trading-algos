"""Prior-day high/low and SMC equilibrium helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class DailyLevels:
    pdh: Optional[float]
    pdl: Optional[float]
    equilibrium: Optional[float]


def prior_day_levels(df: pd.DataFrame, i: int) -> DailyLevels:
    """Return the previous completed daily high/low for bar i."""
    if i <= 0:
        return DailyLevels(None, None, None)

    ts = df.index[i]
    current_day = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
    prev = df.iloc[:i]
    prev_days = prev[prev.index.date < current_day]
    if prev_days.empty:
        return DailyLevels(None, None, None)

    last_day = prev_days.index[-1].date()
    day_slice = prev_days[prev_days.index.date == last_day]
    pdh = float(day_slice['high'].max())
    pdl = float(day_slice['low'].min())
    return DailyLevels(pdh, pdl, (pdh + pdl) / 2.0)


def structure_equilibrium(last_high: float, last_low: float) -> Optional[float]:
    if pd.isna(last_high) or pd.isna(last_low):
        return None
    if last_high in (float('inf'), float('-inf')) or last_low in (float('inf'), float('-inf')):
        return None
    return (float(last_high) + float(last_low)) / 2.0
