"""Binance USDⓈ-M perpetuals as a ``ta_plugin_api.MarketDataProvider``.

The platform contract: instruments, closed candles, and quotes fanned out
through a ``MarketDataHub``. It mirrors the spot plugin (ta_plugin_binance)
with fapi endpoints. Quotes come from ``<s>@bookTicker`` on the ``/public``
route; unlike spot, futures book tickers carry an event time, which the quote
is stamped with.

Order-book depth, trades and funding are not part of the contract; consumers
that need them use :class:`ta_plugin_binance_futures.streams.FuturesStreams`
through the factory's ``streams`` method.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from ta_contracts import TIMEFRAME_MINUTES, Candle, InstrumentInfo, MarketQuote, Timeframe
from ta_core import ServiceError
from ta_core.logging_config import log_event
from ta_plugin_api import MarketDataHub, ProviderCapabilities

from .instruments import PROVIDER_NAME, load_instruments
from .rest import FapiRest, kline_weight
from .streams import WsConnect, _websocket

__all__ = ["INTERVALS", "BinanceFuturesMarketData"]

INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M3: "3m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.H12: "12h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1w",
}
MAX_KLINES = 1500
WEIGHT_BOOK_TICKER_ONE = 2
WEIGHT_BOOK_TICKER_ALL = 5

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BinanceFuturesMarketData:
    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Any,
        *,
        http: httpx.AsyncClient | None = None,
        rest: FapiRest | None = None,
        ws_connect: WsConnect | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        symbols = settings.binance_futures_symbols
        if not symbols:
            raise ValueError("Binance futures market data needs BINANCE_FUTURES_SYMBOLS")
        self._settings = settings
        self._symbols: tuple[str, ...] = symbols
        self._rest = rest or FapiRest(settings, http=http)
        self._owns_rest = rest is None
        self._ws_connect = ws_connect or _websocket
        self._clock = clock
        self._hub = MarketDataHub(queue_size=getattr(settings, "subscriber_queue_size", 256))
        self._instruments: dict[str, InstrumentInfo] = {}
        self._ready = asyncio.Event()
        self._streamer: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._reconnects = 0

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        try:
            self._instruments = await load_instruments(self._rest, self._symbols)
        except ServiceError as exc:
            self._last_error = exc.message
            log_event("binance_futures_start_failed", level=logging.ERROR, error=exc.as_dict())
            self._hub.publish_status("stopped", error=self._last_error)
            return
        self._streamer = asyncio.create_task(self._stream(), name="binance-futures-book-ticker")

    async def wait_ready(self, timeout_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout_seconds)
        return self._ready.is_set()

    async def close(self) -> None:
        if self._streamer is not None:
            self._streamer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._streamer
        if self._owns_rest:
            await self._rest.aclose()
        self._ready.clear()
        self._hub.publish_status("stopped")

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        details: dict[str, Any] = {
            "connected": self._hub.state == "connected",
            "instruments": len(self._instruments),
            "reconnects": self._reconnects,
            "request_weight_used": self._rest.limiter.used,
        }
        if self._last_error:
            details["last_error"] = self._last_error
        blocked = self._rest.limiter.blocked_for()
        if blocked:
            details["rate_limited_for_seconds"] = round(blocked, 1)
        return self._ready.is_set() and self._hub.state == "connected", details

    def feeds(self) -> frozenset[str | None]:
        return frozenset({None})

    # --- reads --------------------------------------------------------------

    def capabilities(self, feed: str | None) -> ProviderCapabilities:
        self._require_feed(feed)
        return ProviderCapabilities(timeframes=tuple(INTERVALS), streaming=True, bid_ask=True)

    def instruments(self, feed: str | None) -> list[InstrumentInfo]:
        self._require_feed(feed)
        self._require_ready()
        return [self._instruments[symbol] for symbol in self._symbols]

    def resolve_symbols(self, feed: str | None, symbols: Iterable[str]) -> frozenset[str]:
        self._require_feed(feed)
        requested = frozenset(symbol.upper() for symbol in symbols)
        unknown = sorted(requested - set(self._symbols))
        if unknown:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                f"Unknown instruments {unknown}",
                {"configured": list(self._symbols)},
            )
        return requested

    async def quote(self, feed: str | None, symbol: str) -> MarketQuote:
        self._require_feed(feed)
        self._require_ready()
        symbol = self._symbol(symbol)
        cached = self._hub.last_quote(symbol)
        if cached is not None and self._hub.state == "connected":
            return cached
        row = await self._rest.get(
            "/fapi/v1/ticker/bookTicker",
            {"symbol": symbol},
            WEIGHT_BOOK_TICKER_ONE,
            unavailable="tick_unavailable",
        )
        quote = self._book_quote(row)
        if quote is None:
            raise ServiceError(503, "tick_unavailable", "Binance returned no valid book ticker")
        return quote

    async def candles(
        self,
        feed: str | None,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        self._require_feed(feed)
        self._require_ready()
        symbol = self._symbol(symbol)
        interval = INTERVALS.get(timeframe)
        if interval is None:
            raise ServiceError(
                422,
                "timeframe_not_supported",
                f"Binance does not serve {timeframe.value}",
                {"supported": [tf.value for tf in INTERVALS]},
            )
        duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        cutoff = min(self._clock(), to) if to is not None else self._clock()
        closed: dict[datetime, Candle] = {}
        # Page backwards; ask for one extra row because the newest may be forming.
        end_ms = int(cutoff.timestamp() * 1000) - 1
        while len(closed) < count:
            limit = min(MAX_KLINES, count - len(closed) + 1)
            rows = await self._rest.get(
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end_ms},
                kline_weight(limit),
                unavailable="candles_unavailable",
            )
            if not rows:
                break
            for row in rows:
                end = datetime.fromtimestamp(int(row[0]) / 1000, UTC) + duration
                if end <= cutoff:
                    closed[end] = Candle(
                        ts=end,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        provider=self.name,
                        source_instrument=symbol,
                        spread_source="unavailable",
                    )
            oldest_open = int(rows[0][0])
            if len(rows) < limit or oldest_open - 1 >= end_ms:
                break
            end_ms = oldest_open - 1
        ordered = sorted(closed.values(), key=lambda candle: candle.ts)
        return ordered[-count:]

    def hub(self, feed: str | None) -> MarketDataHub:
        self._require_feed(feed)
        return self._hub

    # --- the quote stream -----------------------------------------------------

    def stream_url(self) -> str:
        streams = "/".join(f"{symbol.lower()}@bookTicker" for symbol in self._symbols)
        root = self._settings.binance_futures_ws_url.rstrip("/")
        return f"{root}/public/stream?streams={streams}"

    async def _seed_quotes(self) -> None:
        rows = await self._rest.get(
            "/fapi/v1/ticker/bookTicker", {}, WEIGHT_BOOK_TICKER_ALL, unavailable="tick_unavailable"
        )
        for row in rows if isinstance(rows, list) else [rows]:
            quote = self._book_quote(row)
            if quote is not None:
                self._hub.publish_quote(quote)

    async def _stream(self) -> None:
        ceiling = self._settings.binance_futures_reconnect_max_backoff_seconds
        backoff = min(1.0, ceiling)
        while True:
            try:
                async with self._ws_connect(self.stream_url()) as socket:
                    await self._seed_quotes()
                    self._last_error = None
                    self._hub.publish_status("connected")
                    self._ready.set()
                    backoff = min(1.0, ceiling)
                    async for message in socket:
                        if self._on_message(message) == "shutdown":
                            raise ConnectionError("Binance sent serverShutdown")
                raise ConnectionError("Binance closed the stream")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                self._reconnects += 1
                self._hub.publish_status("reconnecting", error=self._last_error)
                log_event(
                    "binance_futures_quotes_reconnecting",
                    level=logging.WARNING,
                    error=self._last_error,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ceiling)

    def _on_message(self, message: str | bytes) -> str | None:
        try:
            payload = json.loads(message)
        except ValueError:
            return None
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        if data.get("e") == "serverShutdown":
            return "shutdown"
        quote = self._book_quote(
            {
                "symbol": data.get("s"),
                "bidPrice": data.get("b"),
                "askPrice": data.get("a"),
                "time": data.get("E"),
            }
        )
        if quote is not None:
            self._hub.publish_quote(quote)
        return None

    def _book_quote(self, row: Any) -> MarketQuote | None:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in self._symbols:
            return None
        try:
            bid = float(row["bidPrice"])
            ask = float(row["askPrice"])
        except (KeyError, TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        event_ms = row.get("time")
        ts = (
            datetime.fromtimestamp(int(event_ms) / 1000, UTC)
            if isinstance(event_ms, int | str) and str(event_ms).isdigit()
            else self._clock()
        )
        return MarketQuote(
            symbol=symbol, source_instrument=symbol, provider=self.name, ts=ts, bid=bid, ask=ask
        )

    # --- guards -------------------------------------------------------------

    def _symbol(self, symbol: str) -> str:
        upper = symbol.upper()
        if upper not in self._symbols:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                f"{symbol} is not configured",
                {"configured": list(self._symbols)},
            )
        return upper

    @staticmethod
    def _require_feed(feed: str | None) -> None:
        if feed is not None:
            raise ServiceError(
                422, "feed_not_supported", "Binance futures serves a single feed; omit the feed"
            )

    def _require_ready(self) -> None:
        if not self._instruments:
            raise ServiceError(503, "broker_not_ready", "Binance instruments are not loaded")
