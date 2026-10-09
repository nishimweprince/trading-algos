"""Value types shared by the core, the backtester and the live runner.

Timestamps are int milliseconds UTC throughout. A bar's ``close_time`` follows
Binance kline semantics (the last millisecond of the interval).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["TF", "Bar", "Side", "Signal", "Swing", "Trend", "Zone"]


class TF(StrEnum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def ms(self) -> int:
        return {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[self.value]


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
    break_times: list[int] = field(default_factory=list)


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
