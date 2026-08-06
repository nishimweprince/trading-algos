"""Provider-neutral candle types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Candle:
    """One closed midpoint candle, timestamped at the end of its UTC interval."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str
    source_instrument: str


@dataclass(frozen=True)
class CandlePage:
    candles: tuple[Candle, ...]
    next_start: datetime | None


class CandleProvider(Protocol):
    name: str

    def validate_instrument(self, instrument: str) -> None:
        """Raise when the configured account cannot access the instrument."""

    def fetch_candles_page(
        self,
        instrument: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> CandlePage:
        """Fetch one bounded page of complete midpoint candles."""
