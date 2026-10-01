from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ta_contracts import Candle, InstrumentInfo, MarketKind, MarketQuote, Timeframe
from ta_core import ServiceError
from ta_plugin_api import MarketDataHub, ProviderCapabilities

from market_data_service.config import MarketBinding, Settings

API_KEY = "test-api-key-at-least-16"
AUTH = {"X-API-Key": API_KEY}


class FakeProvider:
    """An in-memory MarketDataProvider: enough to drive every route."""

    def __init__(
        self,
        name: str = "mt5",
        *,
        feeds: Iterable[str | None] = (None,),
        symbols: Iterable[str] = ("XAUUSD",),
        timeframes: Iterable[Timeframe] = (Timeframe.M5, Timeframe.H1),
    ) -> None:
        self.name = name
        self._feeds = frozenset(feeds)
        self._symbols = frozenset(symbols)
        self._timeframes = tuple(timeframes)
        self._hubs = {feed: MarketDataHub(queue_size=8) for feed in self._feeds}
        self.ready = True
        self.started = False
        self.closed = False
        self.candle_calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.started = True

    async def wait_ready(self, timeout_seconds: float) -> bool:
        return self.ready

    async def close(self) -> None:
        self.closed = True

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        return self.ready, {"connected": self.ready}

    def feeds(self) -> frozenset[str | None]:
        return self._feeds

    def capabilities(self, feed: str | None) -> ProviderCapabilities:
        return ProviderCapabilities(timeframes=self._timeframes, streaming=True, bid_ask=True)

    def instruments(self, feed: str | None) -> list[InstrumentInfo]:
        self._require_ready()
        return [
            InstrumentInfo(symbol=s, source_instrument=f"{s}.b", provider=self.name, digits=2)
            for s in sorted(self._symbols)
        ]

    def resolve_symbols(self, feed: str | None, symbols: Iterable[str]) -> frozenset[str]:
        self._require_ready()
        requested = frozenset(symbols)
        if requested - self._symbols:
            raise ServiceError(422, "symbol_not_allowed", "unknown symbol")
        return requested

    async def quote(self, feed: str | None, symbol: str) -> MarketQuote:
        self._require_ready()
        if symbol not in self._symbols:
            raise ServiceError(422, "symbol_not_allowed", "unknown symbol")
        quote = self._hubs[feed].last_quote(symbol)
        if quote is None:
            raise ServiceError(503, "tick_unavailable", "no quote")
        return quote

    async def candles(
        self,
        feed: str | None,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        self.candle_calls.append(
            {"feed": feed, "symbol": symbol, "timeframe": timeframe, "count": count, "to": to}
        )
        end = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
        return [
            Candle(
                ts=end - timedelta(hours=i),
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                provider=self.name,
                source_instrument=f"{symbol}.b",
            )
            for i in reversed(range(count))
        ]

    def hub(self, feed: str | None) -> MarketDataHub:
        return self._hubs[feed]

    def publish(self, symbol: str, *, feed: str | None = None, age: float = 0.0) -> None:
        self._hubs[feed].publish_quote(
            MarketQuote(
                symbol=symbol,
                source_instrument=f"{symbol}.b",
                provider=self.name,
                ts=datetime.now(UTC) - timedelta(seconds=age),
                bid=2000.0,
                ask=2000.5,
            )
        )

    def _require_ready(self) -> None:
        if not self.ready:
            raise ServiceError(503, "broker_not_ready", "not ready")


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "API_KEY": API_KEY,
        "TOKEN_CACHE_PATH": tmp_path / "token-cache.json",
        "EVENTS_LOG_PATH": tmp_path / "events.jsonl",
        "markets": {MarketKind.FOREX: MarketBinding(provider="mt5")},
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)
