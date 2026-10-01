"""1-minute candles and bid/ask quotes from market-data-service.

A thin adapter over ``ta_clients.MarketDataClient``: the service already returns
closed, UTC, interval-end bars in one contract whatever broker plugin serves the
market, so the only translation left is ipda's own ``Candle``, which is stamped
at the interval *start*.

Only closed 1-minute bars arrive. The aggregator still builds the forming
target-timeframe bucket from them, so a signal can fire mid-bucket, but no
earlier than the close of the minute that triggers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx
from ta_clients import MarketDataClient as _ServiceClient
from ta_contracts import TIMEFRAME_MINUTES, Timeframe

from .candles import Candle
from .config import Settings

_MINUTE = timedelta(minutes=TIMEFRAME_MINUTES[Timeframe.M1])


@dataclass(slots=True)
class Tick:
    symbol: str
    bid: float
    ask: float


class MarketDataClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        api_key = settings.market_data_api_key
        self._service = _ServiceClient(
            settings.market_data_url,
            market=settings.market_data_market,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            client=client,
            timeout_seconds=settings.data_timeout_seconds,
        )

    async def fetch_minute_candles(self, quote: str) -> list[Candle]:
        bars = await self._service.candles(quote, Timeframe.M1, count=self._settings.data_lookback)
        return [
            Candle(
                start=bar.ts - _MINUTE,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                closed=True,
            )
            for bar in bars
        ]

    async def fetch_tick(self, quote: str) -> Tick:
        """Current bid/ask. A provider that does not quote both sides is an error
        here, not a guess: break-even tracking compares against the real side."""
        market_quote = await self._service.quote(quote)
        if market_quote.bid is None or market_quote.ask is None:
            raise ValueError(f"{quote}: market data has no bid/ask, only a last price")
        return Tick(symbol=market_quote.symbol, bid=market_quote.bid, ask=market_quote.ask)
