"""Binance USDⓈ-M stand-ins for consumers' tests.

``FakeBinanceFutures.handler`` is an ``httpx.MockTransport`` handler for the
fapi endpoints the plugin calls. ``FakeFuturesStream`` is a ``ws_connect``
that plays scripted frames per route (``/public`` or ``/market``), one script
per connection, so reconnects can be scripted too. The ``*_frame`` builders
produce combined-stream frames in Binance's wire format.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

__all__ = [
    "NOW",
    "FakeBinanceFutures",
    "FakeFuturesStream",
    "agg_trade_frame",
    "book_ticker_frame",
    "depth_frame",
    "force_order_frame",
    "mark_price_frame",
    "partial_depth_frame",
    "settings",
    "shutdown_frame",
]

NOW = datetime(2026, 10, 1, 10, 7, 30, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
MINUTE_MS = 60_000

EXCHANGE_INFO: dict[str, Any] = {
    "timezone": "UTC",
    "symbols": [
        {
            "symbol": symbol,
            "pair": symbol,
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "baseAsset": symbol.removesuffix("USDT"),
            "quoteAsset": "USDT",
            "pricePrecision": 2,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "1000"},
            ],
        }
        for symbol, tick, step in (("BTCUSDT", "0.10", "0.001"), ("ETHUSDT", "0.01", "0.001"))
    ]
    + [
        {
            "symbol": "BTCUSDT_261225",
            "contractType": "CURRENT_QUARTER",
            "status": "TRADING",
            "filters": [],
        }
    ],
}

BOOK = {
    "BTCUSDT": {"bid": "60000.00", "ask": "60000.10"},
    "ETHUSDT": {"bid": "2500.00", "ask": "2500.01"},
}


def settings(**overrides: Any) -> Any:
    """A settings object carrying every field the plugin reads."""
    from types import SimpleNamespace

    values: dict[str, Any] = {
        "binance_futures_rest_url": "https://fapi.binance.test",
        "binance_futures_ws_url": "wss://fstream.binance.test",
        "binance_futures_symbols": ("BTCUSDT", "ETHUSDT"),
        "binance_futures_request_weight_per_minute": 1200,
        "binance_futures_timeout_seconds": 5.0,
        "binance_futures_reconnect_max_backoff_seconds": 0.01,
        "binance_futures_depth_snapshot_limit": 1000,
        "binance_futures_depth_speed": "0ms",
        "binance_futures_book_mode": "diff",
        "binance_futures_partial_levels": 10,
        "binance_futures_partial_speed": "100ms",
        "binance_futures_book_ticker": None,
        "binance_futures_agg_trades": True,
        "binance_futures_api_key": None,
        "binance_futures_api_secret": None,
        "binance_futures_recv_window_ms": 5000,
        "subscriber_queue_size": 16,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeBinanceFutures:
    """The fapi endpoints the plugin calls, answering like Binance does."""

    def __init__(self, now: datetime = NOW) -> None:
        self.requests: list[httpx.Request] = []
        self.rate_limited = False
        self.used_weight = "7"
        # lastUpdateId per symbol for the next /fapi/v1/depth answer.
        self.snapshot_ids: dict[str, list[int]] = {"BTCUSDT": [100], "ETHUSDT": [500]}
        self.snapshot_levels: dict[str, dict[str, list[list[str]]]] = {
            "BTCUSDT": {
                "bids": [["60000.00", "1.000"], ["59999.90", "2.000"]],
                "asks": [["60000.10", "1.500"], ["60000.20", "3.000"]],
            },
            "ETHUSDT": {
                "bids": [["2500.00", "10.000"]],
                "asks": [["2500.01", "12.000"]],
            },
        }
        last_open = int(now.timestamp() * 1000) // MINUTE_MS * MINUTE_MS
        self.minute_opens = [last_open - i * MINUTE_MS for i in range(3000)][::-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"x-mbx-used-weight-1m": self.used_weight}
        if self.rate_limited:
            return httpx.Response(429, headers={**headers, "retry-after": "30"}, json={})
        path = request.url.path
        params = request.url.params
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, headers=headers, json=EXCHANGE_INFO)
        if path == "/fapi/v1/time":
            return httpx.Response(200, headers=headers, json={"serverTime": NOW_MS})
        if path == "/fapi/v1/ticker/bookTicker":
            rows = [
                {
                    "symbol": symbol,
                    "bidPrice": book["bid"],
                    "bidQty": "1",
                    "askPrice": book["ask"],
                    "askQty": "1",
                    "time": NOW_MS,
                }
                for symbol, book in BOOK.items()
            ]
            if "symbol" in params:
                row = next(r for r in rows if r["symbol"] == params["symbol"])
                return httpx.Response(200, headers=headers, json=row)
            return httpx.Response(200, headers=headers, json=rows)
        if path == "/fapi/v1/depth":
            symbol = params["symbol"]
            ids = self.snapshot_ids[symbol]
            last_update_id = ids.pop(0) if len(ids) > 1 else ids[0]
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "lastUpdateId": last_update_id,
                    "E": NOW_MS,
                    "T": NOW_MS,
                    **self.snapshot_levels[symbol],
                },
            )
        if path == "/fapi/v1/klines":
            if params["symbol"] == "NOPE":
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            end = int(params["endTime"])
            limit = int(params["limit"])
            opens = [t for t in self.minute_opens if t <= end][-limit:]
            rows = [
                [t, "100.0", "101.0", "99.0", str(100 + i % 7), "5.5", t + MINUTE_MS - 1]
                for i, t in enumerate(opens)
            ]
            return httpx.Response(200, headers=headers, json=rows)
        if path in {"/fapi/v1/commissionRate", "/fapi/v1/accountConfig"}:
            if "signature" not in params or "X-MBX-APIKEY" not in request.headers:
                return httpx.Response(401, json={"code": -2014, "msg": "API-key format invalid."})
            if path == "/fapi/v1/commissionRate":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "symbol": params["symbol"],
                        "makerCommissionRate": "0.0002",
                        "takerCommissionRate": "0.0005",
                    },
                )
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "feeTier": 0,
                    "canTrade": False,
                    "canDeposit": True,
                    "canWithdraw": False,
                    "dualSidePosition": False,
                    "multiAssetsMargin": False,
                },
            )
        return httpx.Response(404, json={"code": -1, "msg": path})


class FakeFuturesStream:
    """A ``ws_connect``: the n-th connection to a route plays its n-th script.

    After the last script a connection holds open until ``release`` is set; a
    script ending in ``None`` closes the socket instead (to force a reconnect).
    """

    def __init__(self, scripts: dict[str, Sequence[Sequence[str | None]]]) -> None:
        self.scripts = {route: list(lists) for route, lists in scripts.items()}
        self.urls: list[str] = []
        self.release = asyncio.Event()
        self._connections: dict[str, int] = {}

    def __call__(self, url: str) -> contextlib.AbstractAsyncContextManager[AsyncIterator[str]]:
        self.urls.append(url)
        route = "public" if "/public/" in url else "market" if "/market/" in url else "other"
        index = self._connections.get(route, 0)
        self._connections[route] = index + 1
        scripts = self.scripts.get(route, [])
        frames = list(scripts[index]) if index < len(scripts) else []

        @contextlib.asynccontextmanager
        async def connect() -> AsyncIterator[AsyncIterator[str]]:
            async def play() -> AsyncIterator[str]:
                for frame in frames:
                    if frame is None:
                        return
                    yield frame
                    await asyncio.sleep(0)
                await self.release.wait()

            yield play()

        return connect()


def _frame(stream: str, data: dict[str, Any]) -> str:
    return json.dumps({"stream": stream, "data": data}, separators=(",", ":"))


def depth_frame(
    symbol: str,
    first_id: int,
    final_id: int,
    prev_final_id: int,
    bids: Sequence[tuple[str, str]] = (),
    asks: Sequence[tuple[str, str]] = (),
    event_ms: int = NOW_MS,
) -> str:
    return _frame(
        f"{symbol.lower()}@depth@0ms",
        {
            "e": "depthUpdate",
            "E": event_ms,
            "T": event_ms - 1,
            "s": symbol,
            "U": first_id,
            "u": final_id,
            "pu": prev_final_id,
            "b": [list(level) for level in bids],
            "a": [list(level) for level in asks],
        },
    )


def partial_depth_frame(
    symbol: str,
    final_id: int,
    prev_final_id: int,
    bids: Sequence[tuple[str, str]],
    asks: Sequence[tuple[str, str]],
    levels: int = 10,
    event_ms: int = NOW_MS,
) -> str:
    """A ``<s>@depth<N>@100ms`` frame: Binance labels it ``depthUpdate`` too."""
    return _frame(
        f"{symbol.lower()}@depth{levels}@100ms",
        {
            "e": "depthUpdate",
            "E": event_ms,
            "T": event_ms - 1,
            "s": symbol,
            "ps": symbol,
            "U": prev_final_id + 1,
            "u": final_id,
            "pu": prev_final_id,
            "b": [list(level) for level in bids],
            "a": [list(level) for level in asks],
        },
    )


def book_ticker_frame(
    symbol: str,
    bid: str,
    bid_qty: str,
    ask: str,
    ask_qty: str,
    update_id: int = 1,
    event_ms: int = NOW_MS,
) -> str:
    return _frame(
        f"{symbol.lower()}@bookTicker",
        {
            "e": "bookTicker",
            "u": update_id,
            "E": event_ms,
            "T": event_ms - 1,
            "s": symbol,
            "b": bid,
            "B": bid_qty,
            "a": ask,
            "A": ask_qty,
        },
    )


def agg_trade_frame(
    symbol: str,
    trade_id: int,
    price: str,
    qty: str,
    buyer_is_maker: bool,
    event_ms: int = NOW_MS,
) -> str:
    return _frame(
        f"{symbol.lower()}@aggTrade",
        {
            "e": "aggTrade",
            "E": event_ms,
            "a": trade_id,
            "s": symbol,
            "p": price,
            "q": qty,
            "f": trade_id * 10,
            "l": trade_id * 10 + 1,
            "T": event_ms - 1,
            "m": buyer_is_maker,
        },
    )


def mark_price_frame(
    symbol: str, mark: str, funding_rate: str, next_funding_ms: int, event_ms: int = NOW_MS
) -> str:
    return _frame(
        f"{symbol.lower()}@markPrice@1s",
        {
            "e": "markPriceUpdate",
            "E": event_ms,
            "s": symbol,
            "p": mark,
            "i": mark,
            "P": mark,
            "r": funding_rate,
            "T": next_funding_ms,
        },
    )


def force_order_frame(symbol: str, side: str, price: str, qty: str, event_ms: int = NOW_MS) -> str:
    return _frame(
        f"{symbol.lower()}@forceOrder",
        {
            "e": "forceOrder",
            "E": event_ms,
            "o": {
                "s": symbol,
                "S": side,
                "o": "LIMIT",
                "f": "IOC",
                "q": qty,
                "p": price,
                "ap": price,
                "X": "FILLED",
                "l": qty,
                "z": qty,
                "T": event_ms - 1,
            },
        },
    )


def shutdown_frame() -> str:
    return json.dumps({"e": "serverShutdown", "E": NOW_MS})
