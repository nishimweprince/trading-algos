"""Supply / demand POI zones from the SMC block.

Ports the non-visual pieces of smc.txt lines 597-638 and 884-924:
4-bar sweep setup, optional Mother Bar widening, zone merge, mitigation,
breaker conversion, and stale/swept deletion.
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
class SDZone:
    id: str
    top: float
    bottom: float
    is_bull: bool
    left_index: int
    created_at: datetime
    status: ZoneStatus = ZoneStatus.ACTIVE
    mitigated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.top < self.bottom:
            self.top, self.bottom = self.bottom, self.top

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_active_zone(self) -> ActiveZone:
        return ActiveZone(self.id, ZoneSource.SUPPLY_DEMAND, self.top, self.bottom,
                          self.is_bull, self.status, self.created_at)

    def to_event(self, ts: datetime, status: Optional[ZoneStatus] = None) -> ZoneEvent:
        return ZoneEvent(ts, ZoneSource.SUPPLY_DEMAND, status or self.status,
                         self.id, self.top, self.bottom, self.is_bull,
                         self.created_at)


@dataclass
class SupplyDemandState:
    supply: List[SDZone] = field(default_factory=list)
    demand: List[SDZone] = field(default_factory=list)
    mother_high: float = float('nan')
    mother_low: float = float('nan')
    mother_index: int = 0
    is_sweep_obs: bool = False
    current_obs: int = 0
    high_mobs: float = float('nan')
    low_mobs: float = float('nan')
    is_sweep_obd: bool = False
    current_obd: int = 0
    high_mobd: float = float('nan')
    low_mobd: float = float('nan')

    def active_zones(self) -> List[ActiveZone]:
        zones = [z.as_active_zone() for z in self.supply + self.demand
                 if z.status == ZoneStatus.ACTIVE]
        return zones


def _merge_or_push_zone(zones: List[SDZone], new_zone: SDZone,
                        merge_ratio: float) -> SDZone | None:
    if not zones:
        zones.append(new_zone)
        return new_zone

    last = zones[-1]
    width = max(last.top - last.bottom, 1e-12)
    range_top = abs(new_zone.top - last.top) / width < merge_ratio
    range_bot = abs(new_zone.bottom - last.bottom) / width < merge_ratio
    engulfs = new_zone.top >= last.top and new_zone.bottom <= last.bottom
    inside = new_zone.top <= last.top and new_zone.bottom >= last.bottom

    if engulfs or range_top or range_bot:
        new_zone.top = max(new_zone.top, last.top)
        new_zone.bottom = min(new_zone.bottom, last.bottom)
        new_zone.left_index = last.left_index
        new_zone.created_at = last.created_at
        zones.pop()

    if inside:
        return None
    zones.append(new_zone)
    return new_zone


def _process_zones(state: SupplyDemandState, zones: List[SDZone], is_supply: bool,
                   df: pd.DataFrame, i: int, max_bar_history: int) -> List[ZoneEvent]:
    events: List[ZoneEvent] = []
    ts = index_time(df, i)
    high = float(df['high'].iloc[i])
    low = float(df['low'].iloc[i])
    close = float(df['close'].iloc[i])

    for zone in list(zones):
        if zone.status != ZoneStatus.ACTIVE:
            continue

        if is_supply and low < zone.bottom and close > zone.top:
            zone.status = ZoneStatus.BREAKER
            events.append(zone.to_event(ts, ZoneStatus.BREAKER))
            new_zone = SDZone(str(uuid.uuid4()), zone.top, zone.bottom, True,
                              zone.left_index, zone.created_at)
            state.demand.append(new_zone)
            events.append(new_zone.to_event(ts))
        elif (not is_supply) and high > zone.top and close < zone.bottom:
            zone.status = ZoneStatus.BREAKER
            events.append(zone.to_event(ts, ZoneStatus.BREAKER))
            new_zone = SDZone(str(uuid.uuid4()), zone.top, zone.bottom, False,
                              zone.left_index, zone.created_at)
            state.supply.append(new_zone)
            events.append(new_zone.to_event(ts))
        elif ((is_supply and high >= zone.bottom and high < zone.top)
              or ((not is_supply) and low <= zone.top and low > zone.bottom)):
            if zone.mitigated_at is None:
                zone.mitigated_at = ts
                events.append(zone.to_event(ts, ZoneStatus.MITIGATED))

        stale = i - zone.left_index > max_bar_history
        swept = (is_supply and high >= zone.top) or ((not is_supply) and low <= zone.bottom)
        if stale or swept or zone.status == ZoneStatus.BREAKER:
            zone.status = ZoneStatus.EXPIRED if zone.status != ZoneStatus.BREAKER else zone.status
            if zone in zones:
                zones.remove(zone)
            if stale or swept:
                events.append(zone.to_event(ts, ZoneStatus.EXPIRED))

    return events


def step_supply_demand(state: SupplyDemandState, df: pd.DataFrame, i: int,
                       poi_type: str = '---', merge_ratio: float = 0.0,
                       max_bar_history: int = 2000) -> List[ZoneEvent]:
    events: List[ZoneEvent] = []
    if i == 0:
        state.mother_high = float(df['high'].iloc[i])
        state.mother_low = float(df['low'].iloc[i])
        state.mother_index = i
        return events

    high = float(df['high'].iloc[i])
    low = float(df['low'].iloc[i])
    is_inside = state.mother_high > high and state.mother_low < low
    if not is_inside:
        state.mother_high = high
        state.mother_low = low
        state.mother_index = i

    events.extend(_process_zones(state, state.supply, True, df, i, max_bar_history))
    events.extend(_process_zones(state, state.demand, False, df, i, max_bar_history))

    if i < 4:
        return events

    ts = index_time(df, i)
    highs = df['high']; lows = df['low']

    if not state.is_sweep_obs:
        state.high_mobs = float(highs.iloc[i - 3])
        state.low_mobs = float(lows.iloc[i - 3])
        state.current_obs = i - 3
        if state.high_mobs > highs.iloc[i - 4] and state.high_mobs > highs.iloc[i - 2]:
            state.is_sweep_obs = True
    else:
        if state.low_mobs > highs.iloc[i - 1]:
            zone = SDZone(str(uuid.uuid4()), state.high_mobs, state.low_mobs,
                          False, state.current_obs, index_time(df, state.current_obs))
            pushed = _merge_or_push_zone(state.supply, zone, merge_ratio)
            if pushed:
                events.append(pushed.to_event(ts))
            state.is_sweep_obs = False
        elif poi_type == 'Mother Bar' and state.mother_index <= i - 2:
            state.high_mobs = max(state.high_mobs, state.mother_high)
            state.low_mobs = min(state.low_mobs, state.mother_low)
            state.current_obs = min(state.current_obs, state.mother_index)
        else:
            state.high_mobs = float(highs.iloc[i - 2])
            state.low_mobs = float(lows.iloc[i - 2])
            state.current_obs = i - 2

    if not state.is_sweep_obd:
        state.low_mobd = float(lows.iloc[i - 3])
        state.high_mobd = float(highs.iloc[i - 3])
        state.current_obd = i - 3
        if state.low_mobd < lows.iloc[i - 4] and state.low_mobd < lows.iloc[i - 2]:
            state.is_sweep_obd = True
    else:
        if state.high_mobd < lows.iloc[i - 1]:
            zone = SDZone(str(uuid.uuid4()), state.high_mobd, state.low_mobd,
                          True, state.current_obd, index_time(df, state.current_obd))
            pushed = _merge_or_push_zone(state.demand, zone, merge_ratio)
            if pushed:
                events.append(pushed.to_event(ts))
            state.is_sweep_obd = False
        elif poi_type == 'Mother Bar' and state.mother_index <= i - 2:
            state.high_mobd = max(state.high_mobd, state.mother_high)
            state.low_mobd = min(state.low_mobd, state.mother_low)
            state.current_obd = min(state.current_obd, state.mother_index)
        else:
            state.high_mobd = float(highs.iloc[i - 2])
            state.low_mobd = float(lows.iloc[i - 2])
            state.current_obd = i - 2

    return events


def detect_supply_demand(df: pd.DataFrame, **kwargs) -> tuple[SupplyDemandState, List[ZoneEvent]]:
    state = SupplyDemandState()
    events: List[ZoneEvent] = []
    for i in range(len(df)):
        events.extend(step_supply_demand(state, df, i, **kwargs))
    return state, events
