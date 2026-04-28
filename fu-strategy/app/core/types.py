"""Shared enums and the Signal dataclass.

Minimal placeholder for what step 2 expands. Notification engine consumes
these so it can be wired ahead of the indicator/engine modules.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class Direction(str, Enum):
    BUY = 'BUY'
    SELL = 'SELL'


class Bias(str, Enum):
    BULLISH = 'BULLISH'
    BEARISH = 'BEARISH'
    NEUTRAL = 'NEUTRAL'


class ZoneType(str, Enum):
    SUPPLY = 'SUPPLY'
    DEMAND = 'DEMAND'


class ZoneStatus(str, Enum):
    ACTIVE = 'ACTIVE'
    MITIGATED = 'MITIGATED'
    BREAKER = 'BREAKER'
    EXPIRED = 'EXPIRED'


@dataclass
class Signal:
    id: str
    symbol: str
    timeframe: str
    direction: Direction
    entry_price: float
    sl: float
    tp: float
    structure_bias: Bias
    fu_candle_time: datetime
    zone_id: Optional[str] = None
    confidence: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def rr(self) -> float:
        risk = abs(self.entry_price - self.sl)
        reward = abs(self.tp - self.entry_price)
        return reward / risk if risk > 0 else 0.0
