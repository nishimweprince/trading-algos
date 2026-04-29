"""Shared enums, event records, and the Signal dataclass.

Indicators emit typed events (FUEvent, StructureEvent, ZoneEvent) that the
confluence engine consumes; everything serialises to JSONL via event_log.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


class ZoneSource(str, Enum):
    """Which detector produced a zone — used by confluence to weight signals."""
    FVG = 'FVG'
    SUPPLY_DEMAND = 'SUPPLY_DEMAND'
    MASTER_PATTERN_BOX = 'MASTER_PATTERN_BOX'
    PIVOT_SWING = 'PIVOT_SWING'


@dataclass
class ActiveZone:
    id: str
    source: ZoneSource
    top: float
    bottom: float
    is_bull: bool
    status: ZoneStatus = ZoneStatus.ACTIVE
    created_at: Optional[datetime] = None

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def to_event(self, time: datetime, status: Optional[ZoneStatus] = None) -> 'ZoneEvent':
        return ZoneEvent(
            time=ensure_utc(time),
            source=self.source,
            status=status or self.status,
            id=self.id,
            top=self.top,
            bottom=self.bottom,
            is_bull=self.is_bull,
            created_at=self.created_at,
        )


class StructureEventType(str, Enum):
    BOS = 'BOS'
    CHOCH = 'CHOCH'
    IDM = 'IDM'
    HH = 'HH'
    HL = 'HL'
    LH = 'LH'
    LL = 'LL'
    SCOB = 'SCOB'
    MAJOR_LINE_CONFIRM = 'MAJOR_LINE_CONFIRM'
    MAJOR_LINE_CROSS = 'MAJOR_LINE_CROSS'
    LIQUIDITY_TOUCH = 'LIQUIDITY_TOUCH'


@dataclass
class FUEvent:
    time: datetime
    direction: Direction
    open: float
    high: float
    low: float
    close: float
    leading_doji: bool = False
    sma_aligned: bool = False

    def __post_init__(self) -> None:
        self.time = ensure_utc(self.time)

    def to_log(self) -> Dict[str, Any]:
        return {
            'direction': 'BULL' if self.direction == Direction.BUY else 'BEAR',
            'leading_doji': self.leading_doji,
            'sma_aligned': self.sma_aligned,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
        }


@dataclass
class StructureEvent:
    time: datetime
    type: StructureEventType
    direction: Optional[Direction] = None
    price: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.time = ensure_utc(self.time)

    def to_log(self) -> Dict[str, Any]:
        return {
            'event': self.type.value,
            'direction': self.direction.value if self.direction else None,
            'price': self.price,
            **self.extra,
        }


@dataclass
class ZoneEvent:
    time: datetime
    source: ZoneSource
    status: ZoneStatus
    id: str
    top: float
    bottom: float
    is_bull: bool
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.time = ensure_utc(self.time)
        if self.created_at is not None:
            self.created_at = ensure_utc(self.created_at)
        if self.top < self.bottom:
            self.top, self.bottom = self.bottom, self.top

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_active_zone(self) -> ActiveZone:
        return ActiveZone(
            id=self.id,
            source=self.source,
            top=self.top,
            bottom=self.bottom,
            is_bull=self.is_bull,
            status=self.status,
            created_at=self.created_at,
        )

    def to_log(self) -> Dict[str, Any]:
        return {
            'source': self.source.value,
            'status': self.status.value,
            'id': self.id,
            'top': self.top,
            'bottom': self.bottom,
            'is_bull': self.is_bull,
        }


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
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.fu_candle_time = ensure_utc(self.fu_candle_time)
        self.created_at = ensure_utc(self.created_at)

    @property
    def rr(self) -> float:
        risk = abs(self.entry_price - self.sl)
        reward = abs(self.tp - self.entry_price)
        return reward / risk if risk > 0 else 0.0

    def to_log(self) -> Dict[str, Any]:
        d = asdict(self)
        d['direction'] = self.direction.value
        d['structure_bias'] = self.structure_bias.value
        d['fu_candle_time'] = self.fu_candle_time.isoformat()
        d['created_at'] = self.created_at.isoformat()
        d['rr'] = self.rr
        return d
