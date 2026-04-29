"""Master Pattern contraction boxes and liquidity lines.

Logic-only port of smc.txt lines 971-1296. The TradingView drawing objects are
represented as typed zone/structure events.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Literal, Optional

import pandas as pd

from app.core.types import (
    ActiveZone, Direction, StructureEvent, StructureEventType, ZoneEvent,
    ZoneSource, ZoneStatus,
)
from app.indicators.common import index_time

PivotKind = Literal['high', 'low']


@dataclass
class ZigZagPoint:
    index: int
    time: datetime
    price: float
    kind: PivotKind


@dataclass
class MPBox:
    id: str
    top: float
    bottom: float
    left_index: int
    created_at: datetime
    crossed_high: bool = False
    crossed_low: bool = False
    status: ZoneStatus = ZoneStatus.ACTIVE
    major: bool = False

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_active_zone(self) -> ActiveZone:
        return ActiveZone(self.id, ZoneSource.MASTER_PATTERN_BOX, self.top,
                          self.bottom, True, self.status, self.created_at)

    def to_event(self, ts: datetime, status: Optional[ZoneStatus] = None) -> ZoneEvent:
        return ZoneEvent(ts, ZoneSource.MASTER_PATTERN_BOX, status or self.status,
                         self.id, self.top, self.bottom, True, self.created_at)


@dataclass
class LiquidityLine:
    id: str
    price: float
    side: Literal['buy', 'sell']
    ref_index: int
    crossed: bool = False


@dataclass
class MasterPatternState:
    pivots: List[ZigZagPoint] = field(default_factory=list)
    minor_boxes: List[MPBox] = field(default_factory=list)
    major_boxes: List[MPBox] = field(default_factory=list)
    liquidity_lines: List[LiquidityLine] = field(default_factory=list)
    _last_trigger_signature: Optional[tuple[int, int, int, int]] = None

    def active_zones(self) -> List[ActiveZone]:
        return [b.as_active_zone() for b in self.major_boxes
                if b.status == ZoneStatus.ACTIVE]


def _pivot_kind(df: pd.DataFrame, idx: int, depth: int) -> PivotKind | None:
    if idx - depth < 0 or idx + depth >= len(df):
        return None
    high_win = df['high'].iloc[idx - depth:idx + depth + 1]
    low_win = df['low'].iloc[idx - depth:idx + depth + 1]
    high = df['high'].iloc[idx]
    low = df['low'].iloc[idx]
    is_high = high == high_win.max() and (high_win == high).sum() == 1
    is_low = low == low_win.min() and (low_win == low).sum() == 1
    if is_high and not is_low:
        return 'high'
    if is_low and not is_high:
        return 'low'
    return None


def _add_pivot(state: MasterPatternState, point: ZigZagPoint) -> None:
    if state.pivots and state.pivots[-1].kind == point.kind:
        last = state.pivots[-1]
        if (point.kind == 'high' and point.price > last.price) or (
            point.kind == 'low' and point.price < last.price
        ):
            state.pivots[-1] = point
        return
    state.pivots.append(point)


def _check_trigger(state: MasterPatternState) -> tuple[bool, float, float, int]:
    if len(state.pivots) < 4:
        return False, 0.0, 0.0, 0
    last4 = state.pivots[-4:]
    signature = tuple(p.index for p in last4)
    if signature == state._last_trigger_signature:
        return False, 0.0, 0.0, 0
    # Pine iterates newest->oldest as value1..value4.
    value1, value2, value3, value4 = [p.price for p in reversed(last4)]
    ok = ((value1 < value2 and value1 > value3 and value4 > value2)
          or (value1 > value2 and value1 < value3 and value4 < value2))
    if not ok:
        return False, 0.0, 0.0, 0
    state._last_trigger_signature = signature
    return True, max(value1, value2), min(value1, value2), last4[-2].index


def _highest_since(df: pd.DataFrame, start: int, end: int) -> tuple[int, float]:
    part = df['high'].iloc[start:end + 1]
    idx = int(part.idxmax() if isinstance(part.index[0], int) else part.values.argmax() + start)
    if not isinstance(df.index[0], int):
        idx = int(part.values.argmax() + start)
    return idx, float(df['high'].iloc[idx])


def _lowest_since(df: pd.DataFrame, start: int, end: int) -> tuple[int, float]:
    part = df['low'].iloc[start:end + 1]
    idx = int(part.idxmin() if isinstance(part.index[0], int) else part.values.argmin() + start)
    if not isinstance(df.index[0], int):
        idx = int(part.values.argmin() + start)
    return idx, float(df['low'].iloc[idx])


def _operate_minor_box(state: MasterPatternState, df: pd.DataFrame, i: int) -> tuple[List[ZoneEvent], List[StructureEvent]]:
    zone_events: List[ZoneEvent] = []
    structure_events: List[StructureEvent] = []
    if not state.minor_boxes:
        return zone_events, structure_events

    box = state.minor_boxes[-1]
    if box.status != ZoneStatus.ACTIVE or box.major:
        return zone_events, structure_events
    close = float(df['close'].iloc[i])
    high = float(df['high'].iloc[i])
    low = float(df['low'].iloc[i])
    ts = index_time(df, i)
    orig_high = box.crossed_high
    orig_low = box.crossed_low

    if not box.crossed_high and not box.crossed_low:
        if close > box.top:
            box.crossed_high = True
        if close < box.bottom:
            box.crossed_low = True
    else:
        if high > box.top:
            box.crossed_high = True
        if low < box.bottom:
            box.crossed_low = True

    if box.crossed_high and box.crossed_low:
        box.major = True
        state.major_boxes.append(box)
        zone_events.append(box.to_event(ts, ZoneStatus.ACTIVE))
        mid = box.bottom + (box.top - box.bottom) / 2.0
        structure_events.append(StructureEvent(
            ts, StructureEventType.MAJOR_LINE_CONFIRM, None, mid,
            {'box_id': box.id},
        ))
        hi_idx, hi = _highest_since(df, box.left_index, i)
        lo_idx, lo = _lowest_since(df, box.left_index, i)
        if not orig_high:
            state.liquidity_lines.append(LiquidityLine(str(uuid.uuid4()), lo, 'buy', lo_idx))
        elif not orig_low:
            state.liquidity_lines.append(LiquidityLine(str(uuid.uuid4()), hi, 'sell', hi_idx))
    return zone_events, structure_events


def _operate_liquidity(state: MasterPatternState, df: pd.DataFrame, i: int) -> List[StructureEvent]:
    events: List[StructureEvent] = []
    ts = index_time(df, i)
    high = float(df['high'].iloc[i])
    low = float(df['low'].iloc[i])
    for line in state.liquidity_lines:
        if line.crossed:
            continue
        if line.side == 'buy' and low <= line.price:
            line.crossed = True
            events.append(StructureEvent(ts, StructureEventType.LIQUIDITY_TOUCH,
                                         Direction.BUY, line.price,
                                         {'line_id': line.id}))
        elif line.side == 'sell' and high >= line.price:
            line.crossed = True
            events.append(StructureEvent(ts, StructureEventType.LIQUIDITY_TOUCH,
                                         Direction.SELL, line.price,
                                         {'line_id': line.id}))
    return events


def step_master_pattern(state: MasterPatternState, df: pd.DataFrame, i: int,
                        indi_type: int = 1, max_bars: int = 500) -> tuple[List[ZoneEvent], List[StructureEvent]]:
    zone_events: List[ZoneEvent] = []
    structure_events: List[StructureEvent] = []
    depth = 2 if indi_type == 2 else 3

    pivot_idx = i - depth
    kind = _pivot_kind(df, pivot_idx, depth) if pivot_idx >= 0 else None
    if kind is not None:
        price = float(df['high'].iloc[pivot_idx] if kind == 'high' else df['low'].iloc[pivot_idx])
        _add_pivot(state, ZigZagPoint(pivot_idx, index_time(df, pivot_idx), price, kind))
        ok, top, bottom, pos = _check_trigger(state)
        if ok and i - pos <= max_bars:
            box = MPBox(str(uuid.uuid4()), top, bottom, pos, index_time(df, pos))
            state.minor_boxes.append(box)

    ze, se = _operate_minor_box(state, df, i)
    zone_events.extend(ze)
    structure_events.extend(se)
    structure_events.extend(_operate_liquidity(state, df, i))
    return zone_events, structure_events


def detect_master_pattern(df: pd.DataFrame, **kwargs) -> tuple[MasterPatternState, List[ZoneEvent], List[StructureEvent]]:
    state = MasterPatternState()
    zones: List[ZoneEvent] = []
    structures: List[StructureEvent] = []
    for i in range(len(df)):
        ze, se = step_master_pattern(state, df, i, **kwargs)
        zones.extend(ze)
        structures.extend(se)
    return state, zones, structures
