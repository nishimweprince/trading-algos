"""Inside Bar / Outside Bar / SCOB detection.

Maps smc.txt:
  isb (line 401): motherHigh > high and motherLow < low
  osb (line 663): high > top and low < bot
  scob (lines 640-649): bullish/bearish single-candle order-block confirmation
"""
from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from app.core.types import ActiveZone, Direction, StructureEvent, StructureEventType, ZoneSource
from app.indicators.common import index_time


def is_inside_bar(df: pd.DataFrame, i: int, mother_high: float,
                  mother_low: float) -> bool:
    if i < 1:
        return False
    return mother_high > df['high'].iloc[i] and mother_low < df['low'].iloc[i]


def is_outside_bar(df: pd.DataFrame, i: int, top: float, bot: float) -> bool:
    return df['high'].iloc[i] > top and df['low'].iloc[i] < bot


def detect_scob(df: pd.DataFrame, i: int,
                supply_zones: Optional[Iterable[ActiveZone]] = None,
                demand_zones: Optional[Iterable[ActiveZone]] = None,
                prev_was_inside: bool = False) -> Optional[StructureEvent]:
    """Pine SCOB rule (line 643-649). Requires not-inside-bar at i-1.

    Bullish: low[1] < low[2] and low[1] < low and close > high[1]
             AND low[1] inside an active demand zone.
    Bearish: high[1] > high[2] and high[1] > high and close < low[1]
             AND high[1] inside an active supply zone.
    """
    if i < 2 or prev_was_inside:
        return None
    h = df['high']; l = df['low']; c = df['close']
    ts_py = index_time(df, i)

    bull = (l.iloc[i - 1] < l.iloc[i - 2] and l.iloc[i - 1] < l.iloc[i]
            and c.iloc[i] > h.iloc[i - 1])
    bear = (h.iloc[i - 1] > h.iloc[i - 2] and h.iloc[i - 1] > h.iloc[i]
            and c.iloc[i] < l.iloc[i - 1])

    if bull:
        for zone in demand_zones or []:
            if zone.source == ZoneSource.SUPPLY_DEMAND and zone.is_bull and zone.contains(float(l.iloc[i - 1])):
                return StructureEvent(ts_py, StructureEventType.SCOB,
                                      Direction.BUY, float(l.iloc[i - 1]),
                                      {'zone_id': zone.id})
    if bear:
        for zone in supply_zones or []:
            if zone.source == ZoneSource.SUPPLY_DEMAND and not zone.is_bull and zone.contains(float(h.iloc[i - 1])):
                return StructureEvent(ts_py, StructureEventType.SCOB,
                                      Direction.SELL, float(h.iloc[i - 1]),
                                      {'zone_id': zone.id})
    return None


def detect_scob_legacy(df: pd.DataFrame, i: int,
                       supply_top: Optional[float] = None,
                       supply_bot: Optional[float] = None,
                       demand_top: Optional[float] = None,
                       demand_bot: Optional[float] = None,
                       prev_was_inside: bool = False) -> Optional[StructureEvent]:
    """Compatibility wrapper for older callers that pass one supply/demand box."""
    supply = []
    demand = []
    if supply_top is not None and supply_bot is not None:
        supply.append(ActiveZone('legacy-supply', ZoneSource.SUPPLY_DEMAND,
                                 supply_top, supply_bot, False))
    if demand_top is not None and demand_bot is not None:
        demand.append(ActiveZone('legacy-demand', ZoneSource.SUPPLY_DEMAND,
                                 demand_top, demand_bot, True))
    return detect_scob(df, i, supply, demand, prev_was_inside)
