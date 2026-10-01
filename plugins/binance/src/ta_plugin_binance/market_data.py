"""Binance Spot as a ``ta_plugin_api.MarketDataProvider``.

One process-wide connection, so one feed (``None``). Canonical symbols are
Binance's own (``BTCUSDT``), configured with ``BINANCE_SYMBOLS``.

Two things Binance returns that the platform contract does not accept:

- **Interval start.** A kline row is keyed by its open time; the contract
  stamps a bar at its interval *end*, so the duration is added.
- **The forming bar.** The newest row is still open. Rows whose interval has
  not ended (relative to now, or to ``to``) are dropped.

Quotes come from the combined ``<symbol>@bookTicker`` stream, which carries
best bid/ask but no event time, so a quote is stamped when it is received. The
REST book ticker seeds the cache at start and after every reconnect, so a
reconnect cannot leave a stale quote looking current.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from ta_contracts import TIMEFRAME_MINUTES, Candle, InstrumentInfo, MarketQuote, Timeframe
from ta_core import ServiceError
from ta_core.logging_config import log_event
from ta_plugin_api import MarketDataHub, ProviderCapabilities

from .limiter import WeightLimiter

__all__ = ["INTERVALS", "BinanceMarketData", "WsConnect"]

# Platform timeframe -> Binance kline interval. Binance has no M2/M4/M10.
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
MAX_KLINES = 1000

# Request weights, per the Binance Spot REST documentation.
WEIGHT_EXCHANGE_INFO = 20
WEIGHT_KLINES = 2
WEIGHT_BOOK_TICKER_ONE = 2
WEIGHT_BOOK_TICKER_MANY = 4

Clock = Callable[[], datetime]
WsConnect = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextlib.asynccontextmanager
async def _websocket(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
    from websockets.asyncio.client import connect

    async with connect(url, ping_interval=20, ping_timeout=20, max_queue=1024) as socket:
        yield socket


def _digits(tick_size: str) -> int:
    exponent = Decimal(tick_size).normalize().as_tuple().exponent
    return max(0, -int(exponent))


class BinanceMarketData:
    name = "binance"

    def __init__(
        self,
        settings: Any,
        *,
        http: httpx.AsyncClient | None = None,
        ws_connect: WsConnect | None = None,
        clock: Clock = _utc_now,
        limiter: WeightLimiter | None = None,
    ) -> None:
        symbols = settings.binance_symbols
        if not symbols:
            raise ValueError("Binance market data needs BINANCE_SYMBOLS")
        self._settings = settings
        self._symbols: tuple[str, ...] = symbols
        self._http = http or httpx.AsyncClient(
            base_url=settings.binance_rest_url, timeout=settings.binance_timeout_seconds
        )
        self._owns_http = http is None
        self._ws_connect = ws_connect or _websocket
        self._clock = clock
        self._limiter = limiter or WeightLimiter(settings.binance_request_weight_per_minute)
        self._hub = MarketDataHub(queue_size=getattr(settings, "subscriber_queue_size", 256))
        self._instruments: dict[str, InstrumentInfo] = {}
        self._ready = asyncio.Event()
        self._streamer: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._reconnects = 0

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        try:
            await self._load_instruments()
        except (ServiceError, httpx.HTTPError) as exc:
            self._last_error = str(exc)
            log_event("binance_start_failed", level=logging.ERROR, error=str(exc))
            self._hub.publish_status("stopped", error=self._last_error)
            return
        self._streamer = asyncio.create_task(self._stream(), name="binance-book-ticker")

    async def wait_ready(self, timeout_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout_seconds)
        return self._ready.is_set()

    async def close(self) -> None:
        if self._streamer is not None:
            self._streamer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._streamer
        if self._owns_http:
            await self._http.aclose()
        self._ready.clear()
        self._hub.publish_status("stopped")

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        details: dict[str, Any] = {
            "connected": self._hub.state == "connected",
            "instruments": len(self._instruments),
            "reconnects": self._reconnects,
            "request_weight_used": self._limiter.used,
        }
        if self._last_error:
            details["last_error"] = self._last_error
        blocked = self._limiter.blocked_for()
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
        rows = await self._get(
            "/api/v3/ticker/bookTicker",
            {"symbol": symbol},
            WEIGHT_BOOK_TICKER_ONE,
            unavailable="tick_unavailable",
        )
        quote = self._book_quote(rows)
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
        # Page backwards from the cutoff. Each request asks for one more row
        # than still needed, because the newest row may be the forming bar.
        end_ms = int(cutoff.timestamp() * 1000) - 1
        while len(closed) < count:
            limit = min(MAX_KLINES, count - len(closed) + 1)
            rows = await self._get(
                "/api/v3/klines",
                {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end_ms},
                WEIGHT_KLINES,
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
                break  # the exchange has no older history for this symbol
            end_ms = oldest_open - 1
        ordered = sorted(closed.values(), key=lambda candle: candle.ts)
        return ordered[-count:]

    def hub(self, feed: str | None) -> MarketDataHub:
        self._require_feed(feed)
        return self._hub

    # --- REST ---------------------------------------------------------------

    async def _get(
        self, path: str, params: dict[str, Any], weight: int, *, unavailable: str
    ) -> Any:
        blocked = self._limiter.blocked_for()
        if blocked:
            raise ServiceError(
                503,
                unavailable,
                "Binance rate limit in force",
                {"retry_after_seconds": round(blocked, 1)},
            )
        await self._limiter.acquire(weight)
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ServiceError(
                503, unavailable, "Binance did not answer", {"reason": type(exc).__name__}
            ) from exc
        used = response.headers.get("x-mbx-used-weight-1m")
        self._limiter.observe(int(used) if used and used.isdigit() else None)
        if response.status_code in {418, 429}:
            retry_after = float(response.headers.get("retry-after") or 60)
            self._limiter.block(retry_after)
            log_event(
                "binance_rate_limited",
                level=logging.ERROR,
                status=response.status_code,
                retry_after_seconds=retry_after,
            )
            raise ServiceError(
                503,
                unavailable,
                "Binance rate limit exceeded",
                {"status": response.status_code, "retry_after_seconds": retry_after},
            )
        if response.status_code >= 400:
            detail = _error_body(response)
            raise ServiceError(
                503,
                unavailable,
                "Binance rejected the request",
                {"status": response.status_code, **detail},
            )
        return response.json()

    async def _load_instruments(self) -> None:
        payload = await self._get(
            "/api/v3/exchangeInfo",
            {"symbols": json.dumps(list(self._symbols), separators=(",", ":"))},
            WEIGHT_EXCHANGE_INFO,
            unavailable="broker_not_ready",
        )
        found = {row["symbol"]: row for row in payload.get("symbols", [])}
        missing = sorted(set(self._symbols) - set(found))
        if missing:
            raise ServiceError(
                503, "broker_not_ready", f"Binance does not list {missing}", {"missing": missing}
            )
        for symbol in self._symbols:
            row = found[symbol]
            filters = {item["filterType"]: item for item in row.get("filters", [])}
            price = filters.get("PRICE_FILTER", {})
            lot = filters.get("LOT_SIZE", {})
            tick = price.get("tickSize")
            self._instruments[symbol] = InstrumentInfo(
                symbol=symbol,
                source_instrument=symbol,
                provider=self.name,
                digits=_digits(tick) if tick else int(row.get("quotePrecision", 8)),
                description=f"{row.get('baseAsset')}/{row.get('quoteAsset')}",
                price_increment=float(tick) if tick else None,
                quantity_increment=float(lot["stepSize"]) if "stepSize" in lot else None,
                min_quantity=float(lot["minQty"]) if "minQty" in lot else None,
                max_quantity=float(lot["maxQty"]) if "maxQty" in lot else None,
            )

    async def _seed_quotes(self) -> None:
        rows = await self._get(
            "/api/v3/ticker/bookTicker",
            {"symbols": json.dumps(list(self._symbols), separators=(",", ":"))},
            WEIGHT_BOOK_TICKER_MANY,
            unavailable="tick_unavailable",
        )
        for row in rows if isinstance(rows, list) else [rows]:
            quote = self._book_quote(row)
            if quote is not None:
                self._hub.publish_quote(quote)

    # --- the stream -----------------------------------------------------------

    def stream_url(self) -> str:
        streams = "/".join(f"{symbol.lower()}@bookTicker" for symbol in self._symbols)
        return f"{self._settings.binance_ws_url.rstrip('/')}/stream?streams={streams}"

    async def _stream(self) -> None:
        ceiling = self._settings.binance_reconnect_max_backoff_seconds
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
                        self._on_message(message)
                raise ConnectionError("Binance closed the stream")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                self._reconnects += 1
                self._hub.publish_status("reconnecting", error=self._last_error)
                log_event(
                    "binance_stream_reconnecting",
                    level=logging.WARNING,
                    error=self._last_error,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ceiling)

    def _on_message(self, message: str | bytes) -> None:
        try:
            payload = json.loads(message)
        except ValueError:
            return
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        quote = self._book_quote(
            {
                "symbol": data.get("s"),
                "bidPrice": data.get("b"),
                "askPrice": data.get("a"),
            }
        )
        if quote is not None:
            self._hub.publish_quote(quote)

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
        return MarketQuote(
            symbol=symbol,
            source_instrument=symbol,
            provider=self.name,
            ts=self._clock(),
            bid=bid,
            ask=ask,
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
                422, "feed_not_supported", "Binance serves a single feed; omit the feed"
            )

    def _require_ready(self) -> None:
        if not self._instruments:
            raise ServiceError(503, "broker_not_ready", "Binance instruments are not loaded")


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    if isinstance(body, dict):
        return {key: body[key] for key in ("code", "msg") if key in body}
    return {}
