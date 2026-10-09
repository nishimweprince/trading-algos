"""Value types shared by the core, the backtester and the live runner.

Timestamps are int milliseconds UTC throughout. A bar's ``close_time`` follows
Binance kline semantics (the last millisecond of the interval).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["TF", "Bar", "Position", "Rejected", "Side", "Signal", "Swing", "Trend", "Zone"]


class TF(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def ms(self) -> int:
        return {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[self.value]


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


class Trend(StrEnum):
    UP = "up"
    DOWN = "down"
    RANGE = "range"


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Swing:
    tf: TF
    kind: str  # "high" | "low"
    pivot_time: int
    price: float
    t_conf: int  # close time of the confirming bar; invisible before this
    atr: float
    body: float = 0.0  # |close - open| of the pivot candle, for zone width


@dataclass(slots=True)
class Zone:
    id: str
    tf_origin: set[TF]
    lo: float
    hi: float
    role: str  # "support" | "resistance"
    strength: float
    touches: int
    created: int
    broken_at: int | None = None
    flips: int = 0
    deep_retests: int = 0
    breaks: list[tuple[int, str]] = field(default_factory=list)  # (4H close time, "up"|"down")
    seeds: frozenset[str] = frozenset()  # "{tf}:{kind}:{pivot_time}" of the swings it came from
    extra_touches: int = 0  # visits that ended back on the role side, after creation
    in_visit: bool = False
    visit_deep: bool = False

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2


@dataclass(frozen=True, slots=True)
class Signal:
    id: str  # sha1(symbol, zone_id, entry_type, bar_time)
    symbol: str
    side: Side
    entry_type: str  # "A" | "B" | "C"
    bar_time: int
    entry_ref: float
    stop: float
    tp1: float
    tp2: float | None
    quality: int
    zone_id: str


@dataclass(frozen=True, slots=True)
class Rejected:
    """A candidate the core or risk layer turned down, kept for signals.parquet."""

    symbol: str
    side: Side
    entry_type: str
    zone_id: str
    bar_time: int
    reason: str
    detail: str = ""


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    signal_id: str
    entry: float
    stop: float
    stop_initial: float
    tp1: float
    tp2: float | None
    qty: float
    qty_open: float
    opened_at: int  # fill time, ms
    tp1_done: bool = False
    bars_open: int = 0
    best: float = 0.0  # most favourable price since entry (high for longs)
    worst: float = 0.0
    zone_id: str = ""

    def __post_init__(self) -> None:
        if not self.best:
            self.best = self.entry
        if not self.worst:
            self.worst = self.entry

    @property
    def r(self) -> float:
        return abs(self.entry - self.stop_initial)

    @property
    def mfe_r(self) -> float:
        return self.side.sign * (self.best - self.entry) / self.r if self.r else 0.0

    @property
    def mae_r(self) -> float:
        return self.side.sign * (self.entry - self.worst) / self.r if self.r else 0.0
