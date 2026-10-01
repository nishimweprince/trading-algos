"""A consumer-owned JSONL candle cache.

One file per (symbol, timeframe), one ``Candle`` per line, oldest first. Backtests
read it and never touch the network; ``MarketDataClient`` is what fills it. The
path layout belongs to the consumer, so it is passed in rather than assumed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from ta_contracts import Candle, Timeframe

__all__ = ["JsonlCandleCache", "filter_candles"]

PathFor = Callable[[str, Timeframe], Path]


class JsonlCandleCache:
    def __init__(self, path_for: PathFor) -> None:
        self._path_for = path_for

    def path(self, symbol: str, timeframe: Timeframe) -> Path:
        return self._path_for(symbol, timeframe)

    def exists(self, symbol: str, timeframe: Timeframe) -> bool:
        path = self.path(symbol, timeframe)
        return path.is_file() and path.stat().st_size > 0

    def load(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        count: int | None = None,
    ) -> list[Candle]:
        path = self.path(symbol, timeframe)
        if not path.is_file():
            return []
        candles: list[Candle] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    candles.append(Candle.model_validate_json(line))
        return filter_candles(candles, date_from=date_from, date_to=date_to, count=count)

    def write(self, symbol: str, timeframe: Timeframe, candles: Iterable[Candle]) -> Path:
        path = self.path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(candles, key=lambda c: c.ts)
        with path.open("w", encoding="utf-8") as handle:
            for candle in ordered:
                handle.write(candle.model_dump_json() + "\n")
        return path


def filter_candles(
    candles: Iterable[Candle],
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    count: int | None = None,
) -> list[Candle]:
    out = list(candles)
    if date_from is not None:
        out = [c for c in out if c.ts >= date_from]
    if date_to is not None:
        out = [c for c in out if c.ts <= date_to]
    out = sorted(out, key=lambda c: c.ts)
    if count is not None and len(out) > count:
        out = out[-count:]
    return out
