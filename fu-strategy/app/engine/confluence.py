"""FU + HTF zone + HTF bias confluence."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional, Sequence

import pandas as pd

from app.core.types import ActiveZone, Bias, Direction, FUEvent, Signal
from app.indicators.common import atr


@dataclass(frozen=True)
class ConfluenceDecision:
    signal: Optional[Signal]
    reason: str
    matched_zones: List[ActiveZone]


def direction_bias(direction: Direction) -> Bias:
    return Bias.BULLISH if direction == Direction.BUY else Bias.BEARISH


def build_signal(
    *,
    symbol: str,
    ltf_timeframe: str,
    fu: FUEvent,
    ltf_df: pd.DataFrame,
    htf_biases: dict[str, Bias],
    htf_zones: dict[str, Sequence[ActiveZone]],
    rr_target: float = 2.0,
    atr_fraction: float = 0.1,
    fu_only: bool = False,
) -> ConfluenceDecision:
    needed_bias = direction_bias(fu.direction)

    if fu_only:
        entry = fu.close
        atr_value = float(atr(ltf_df).iloc[-1]) if not ltf_df.empty else abs(fu.high - fu.low)
        buffer = atr_value * atr_fraction
        if fu.direction == Direction.BUY:
            sl = fu.low - buffer
            tp = entry + abs(entry - sl) * rr_target
        else:
            sl = fu.high + buffer
            tp = entry - abs(sl - entry) * rr_target
        signal = Signal(
            id=str(uuid.uuid4()),
            symbol=symbol,
            timeframe=ltf_timeframe,
            direction=fu.direction,
            entry_price=float(entry),
            sl=float(sl),
            tp=float(tp),
            structure_bias=needed_bias,
            fu_candle_time=fu.time,
            zone_id=None,
            confidence=[f"FU {needed_bias.value}", 'fu_only_mode'],
        )
        return ConfluenceDecision(signal, 'signal emitted (fu_only)', [])

    agreeing_tfs = [tf for tf, bias in htf_biases.items() if bias == needed_bias]
    if not agreeing_tfs:
        return ConfluenceDecision(None, 'HTF bias does not agree with FU direction', [])

    entry = fu.close
    all_zones: List[ActiveZone] = []
    for tf in agreeing_tfs:
        all_zones.extend(htf_zones.get(tf, []))
    matched = [z for z in all_zones if z.contains(entry)]
    if not matched:
        return ConfluenceDecision(None, 'FU entry is outside active HTF zones', [])

    atr_value = float(atr(ltf_df).iloc[-1]) if not ltf_df.empty else abs(fu.high - fu.low)
    buffer = atr_value * atr_fraction
    if fu.direction == Direction.BUY:
        sl = fu.low - buffer
        tp = _nearest_opposing_zone(entry, all_zones, Direction.BUY)
        if tp is None or tp <= entry:
            tp = entry + abs(entry - sl) * rr_target
    else:
        sl = fu.high + buffer
        tp = _nearest_opposing_zone(entry, all_zones, Direction.SELL)
        if tp is None or tp >= entry:
            tp = entry - abs(sl - entry) * rr_target

    confidence = [
        f"FU {needed_bias.value}",
        f"HTF bias {','.join(agreeing_tfs)} {needed_bias.value}",
        *[f"{z.source.value} zone {z.id}" for z in matched],
    ]
    signal = Signal(
        id=str(uuid.uuid4()),
        symbol=symbol,
        timeframe=ltf_timeframe,
        direction=fu.direction,
        entry_price=float(entry),
        sl=float(sl),
        tp=float(tp),
        structure_bias=needed_bias,
        fu_candle_time=fu.time,
        zone_id=matched[0].id,
        confidence=confidence,
    )
    return ConfluenceDecision(signal, 'signal emitted', matched)


def _nearest_opposing_zone(entry: float, zones: Sequence[ActiveZone],
                           direction: Direction) -> Optional[float]:
    if direction == Direction.BUY:
        candidates = [z.bottom for z in zones if not z.is_bull and z.bottom > entry]
        return min(candidates) if candidates else None
    candidates = [z.top for z in zones if z.is_bull and z.top < entry]
    return max(candidates) if candidates else None
