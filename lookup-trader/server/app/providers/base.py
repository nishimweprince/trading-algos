"""Provider-neutral candle types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    """One validated closed candle, timestamped at the end of its UTC interval."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str
    source_instrument: str
    spread: float | None = None
    spread_source: str | None = None
