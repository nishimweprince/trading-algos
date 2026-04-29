"""Fair Value Gap detection — port of smc.txt FVG block (lines 8-225).

Pine rule (3-bar gap):
  bull_fvg = low > high[2] and close[1] > high[2] and (low - high[2]) / high[2] > threshold
  bear_fvg = high < low[2] and close[1] < low[2] and (low[2] - high) / high > threshold

Mitigation:
  bull mitigated when close < zone.min
  bear mitigated when close > zone.max
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Tuple

import pandas as pd

from app.core.types import ActiveZone, ZoneEvent, ZoneSource, ZoneStatus
from app.indicators.common import index_time


@dataclass
class FVGZone:
    id: str
    is_bull: bool
    top: float        # = .max
    bottom: float     # = .min
    created_at: datetime
    status: ZoneStatus = ZoneStatus.ACTIVE
    mitigated_at: datetime | None = None

    def to_event(self, ts: datetime) -> ZoneEvent:
        return ZoneEvent(
            time=ts, source=ZoneSource.FVG, status=self.status,
            id=self.id, top=self.top, bottom=self.bottom,
            is_bull=self.is_bull, created_at=self.created_at,
        )

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_active_zone(self) -> ActiveZone:
        return ActiveZone(
            id=self.id, source=ZoneSource.FVG, top=self.top, bottom=self.bottom,
            is_bull=self.is_bull, status=self.status, created_at=self.created_at,
        )


@dataclass
class FVGState:
    """Per-(symbol, timeframe) FVG accumulator."""
    zones: List[FVGZone] = field(default_factory=list)
    bull_count: int = 0
    bear_count: int = 0
    bull_mitigated: int = 0
    bear_mitigated: int = 0
    # for auto-threshold accumulation: cum((H-L)/L), plus Pine-like bar_index
    _cum_range_pct: float = 0.0
    _last_bar_index: int = 0

    def auto_threshold(self) -> float:
        if self._last_bar_index <= 0:
            return 0.0
        return self._cum_range_pct / self._last_bar_index

    def active_zones(self) -> List[FVGZone]:
        return [z for z in self.zones if z.status == ZoneStatus.ACTIVE]

    def active_zone_events(self) -> List[ActiveZone]:
        return [z.as_active_zone() for z in self.active_zones()]


def _detect_one(df: pd.DataFrame, i: int, threshold: float) -> Tuple[bool, bool]:
    """Pine: indices i-2 (high[2]), i-1 (close[1]), i (low/high)."""
    h2 = df['high'].iloc[i - 2]
    l2 = df['low'].iloc[i - 2]
    c1 = df['close'].iloc[i - 1]
    li = df['low'].iloc[i]
    hi = df['high'].iloc[i]

    bull = (li > h2) and (c1 > h2) and ((li - h2) / h2 > threshold) if h2 != 0 else False
    bear = (hi < l2) and (c1 < l2) and ((l2 - hi) / hi > threshold) if hi != 0 else False
    return bull, bear


def step_fvg(state: FVGState, df: pd.DataFrame, i: int,
             threshold_pct: float = 0.0,
             auto: bool = False) -> List[ZoneEvent]:
    """Process a single bar at index i. Returns events (new + mitigated)."""
    events: List[ZoneEvent] = []
    if i < 2:
        if auto:
            l = df['low'].iloc[i]
            h = df['high'].iloc[i]
            if l > 0:
                state._cum_range_pct += (h - l) / l
            state._last_bar_index = i
        return events

    if auto:
        l = df['low'].iloc[i]
        h = df['high'].iloc[i]
        if l > 0:
            state._cum_range_pct += (h - l) / l
        state._last_bar_index = i
        threshold = state.auto_threshold()
    else:
        threshold = threshold_pct / 100.0

    bull, bear = _detect_one(df, i, threshold)
    ts_py = index_time(df, i)

    if bull:
        z = FVGZone(id=str(uuid.uuid4()), is_bull=True,
                    top=float(df['high'].iloc[i - 2]),
                    bottom=float(df['low'].iloc[i]),
                    created_at=ts_py)
        # Pine: max=low (current low), min=high[2] -> bull zone is between high[2] (bot) and low (top)
        # but Pine fvg.new(low, high[2], true) means max=low, min=high[2]; for bull this puts max above min.
        # Reorder so top >= bottom.
        if z.top < z.bottom:
            z.top, z.bottom = z.bottom, z.top
        state.zones.append(z)
        state.bull_count += 1
        events.append(z.to_event(ts_py))

    if bear:
        z = FVGZone(id=str(uuid.uuid4()), is_bull=False,
                    top=float(df['low'].iloc[i - 2]),
                    bottom=float(df['high'].iloc[i]),
                    created_at=ts_py)
        if z.top < z.bottom:
            z.top, z.bottom = z.bottom, z.top
        state.zones.append(z)
        state.bear_count += 1
        events.append(z.to_event(ts_py))

    # Mitigation pass
    close = float(df['close'].iloc[i])
    for z in state.zones:
        if z.status != ZoneStatus.ACTIVE:
            continue
        if z.is_bull and close < z.bottom:
            z.status = ZoneStatus.MITIGATED
            z.mitigated_at = ts_py
            state.bull_mitigated += 1
            events.append(z.to_event(ts_py))
        elif (not z.is_bull) and close > z.top:
            z.status = ZoneStatus.MITIGATED
            z.mitigated_at = ts_py
            state.bear_mitigated += 1
            events.append(z.to_event(ts_py))

    return events


def detect_fvg(df: pd.DataFrame,
               threshold_pct: float = 0.0,
               auto: bool = False) -> Tuple[FVGState, List[ZoneEvent]]:
    """Convenience: full backfill in one shot."""
    state = FVGState()
    events: List[ZoneEvent] = []
    for i in range(len(df)):
        events.extend(step_fvg(state, df, i, threshold_pct, auto))
    return state, events
