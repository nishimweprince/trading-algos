"""Pivot swing level boxes from smc.txt lines 1299-1465.

The crypto open-interest filter is intentionally omitted. Volume filtering is
kept because OHLCV data from Capital.com can provide it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd

from app.core.types import ActiveZone, ZoneEvent, ZoneSource, ZoneStatus
from app.indicators.common import index_time


@dataclass
class PivotSwingZone:
    id: str
    level: float
    top: float
    bottom: float
    is_bull: bool
    left_index: int
    created_at: datetime
    status: ZoneStatus = ZoneStatus.ACTIVE
    filled_at: Optional[datetime] = None

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_active_zone(self) -> ActiveZone:
        return ActiveZone(self.id, ZoneSource.PIVOT_SWING, self.top, self.bottom,
                          self.is_bull, self.status, self.created_at)

    def to_event(self, ts: datetime, status: Optional[ZoneStatus] = None) -> ZoneEvent:
        return ZoneEvent(ts, ZoneSource.PIVOT_SWING, status or self.status,
                         self.id, self.top, self.bottom, self.is_bull,
                         self.created_at)


@dataclass
class PivotSwingState:
    zones: List[PivotSwingZone] = field(default_factory=list)

    def active_zones(self) -> List[ActiveZone]:
        return [z.as_active_zone() for z in self.zones if z.status == ZoneStatus.ACTIVE]


def _is_pivot_high(series: pd.Series, idx: int, left: int, right: int) -> bool:
    if idx - left < 0 or idx + right >= len(series):
        return False
    value = series.iloc[idx]
    window = series.iloc[idx - left:idx + right + 1]
    return bool(value == window.max() and (window == value).sum() == 1)


def _is_pivot_low(series: pd.Series, idx: int, left: int, right: int) -> bool:
    if idx - left < 0 or idx + right >= len(series):
        return False
    value = series.iloc[idx]
    window = series.iloc[idx - left:idx + right + 1]
    return bool(value == window.min() and (window == value).sum() == 1)


def _box_bounds(level: float, is_bull: bool, box_width: float = 0.7,
                box_style: str = 'TYPE 1') -> tuple[float, float]:
    box_wid = 0.001 * box_width
    if box_style == 'TYPE 2':
        other = level * (1 + box_wid) if is_bull else level * (1 - box_wid)
    else:
        other = level
    top = max(level, other)
    bottom = min(level, other)
    if top == bottom:
        pad = abs(level) * box_wid or box_wid
        top = level + pad
        bottom = level - pad
    return top, bottom


def step_pivot_swing(state: PivotSwingState, df: pd.DataFrame, i: int,
                     swing_size_l: int = 15, swing_size_r: int = 10,
                     hide_filled: bool = False,
                     extend_til_filled: bool = True,
                     volume_threshold: float = 0.0,
                     box_width: float = 0.7,
                     box_style: str = 'TYPE 1') -> List[ZoneEvent]:
    events: List[ZoneEvent] = []
    ts = index_time(df, i)

    for zone in list(state.zones):
        if zone.status != ZoneStatus.ACTIVE:
            continue
        filled = float(df['high'].iloc[i]) >= zone.level and float(df['low'].iloc[i]) <= zone.level
        if filled and i > zone.left_index:
            zone.filled_at = ts
            zone.status = ZoneStatus.MITIGATED
            events.append(zone.to_event(ts, ZoneStatus.MITIGATED))
            if hide_filled or extend_til_filled:
                state.zones.remove(zone)
        elif not extend_til_filled and i > zone.left_index:
            zone.status = ZoneStatus.EXPIRED
            events.append(zone.to_event(ts, ZoneStatus.EXPIRED))
            state.zones.remove(zone)

    pivot_idx = i - swing_size_r
    if pivot_idx < 0:
        return events
    if 'volume' in df.columns and volume_threshold > 0:
        if float(df['volume'].iloc[pivot_idx]) <= volume_threshold:
            return events

    if _is_pivot_high(df['high'], pivot_idx, swing_size_l, swing_size_r):
        level = float(df['high'].iloc[pivot_idx])
        top, bottom = _box_bounds(level, False, box_width, box_style)
        zone = PivotSwingZone(str(uuid.uuid4()), level, top, bottom, False,
                              pivot_idx, index_time(df, pivot_idx))
        state.zones.append(zone)
        events.append(zone.to_event(ts))

    if _is_pivot_low(df['low'], pivot_idx, swing_size_l, swing_size_r):
        level = float(df['low'].iloc[pivot_idx])
        top, bottom = _box_bounds(level, True, box_width, box_style)
        zone = PivotSwingZone(str(uuid.uuid4()), level, top, bottom, True,
                              pivot_idx, index_time(df, pivot_idx))
        state.zones.append(zone)
        events.append(zone.to_event(ts))

    while len(state.zones) >= 500:
        zone = state.zones.pop(0)
        zone.status = ZoneStatus.EXPIRED
        events.append(zone.to_event(ts, ZoneStatus.EXPIRED))

    return events


def detect_pivot_swing(df: pd.DataFrame, **kwargs) -> tuple[PivotSwingState, List[ZoneEvent]]:
    state = PivotSwingState()
    events: List[ZoneEvent] = []
    for i in range(len(df)):
        events.extend(step_pivot_swing(state, df, i, **kwargs))
    return state, events
