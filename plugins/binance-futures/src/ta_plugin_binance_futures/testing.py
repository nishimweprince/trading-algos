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
from decimal import Decimal
from typing import Any

import httpx

__all__ = [
    "NOW",
    "FakeBinanceFutures",
    "FakeBinanceTrading",
    "FakeFuturesStream",
    "FakeUserStream",
    "agg_trade_frame",
    "book_ticker_frame",
    "depth_frame",
    "force_order_frame",
    "mark_price_frame",
    "partial_depth_frame",
    "settings",
    "shutdown_frame",
    "trading_settings",
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
        self.key_restrictions: dict[str, Any] = {
            "ipRestrict": True,
            "createTime": NOW_MS,
            "enableReading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableVanillaOptions": False,
            "enableFutures": False,
            "enableMargin": False,
            "enableSpotAndMarginTrading": False,
            "enablePortfolioMarginTrading": False,
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
        if path == "/sapi/v1/account/apiRestrictions":
            if "signature" not in params or "X-MBX-APIKEY" not in request.headers:
                return httpx.Response(401, json={"code": -2014, "msg": "API-key format invalid."})
            return httpx.Response(200, headers=headers, json=self.key_restrictions)
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


# --- order entry ---------------------------------------------------------------


def trading_settings(**overrides: Any) -> Any:
    """``settings()`` plus what the execution adapter and the gateway read."""
    from pydantic import SecretStr

    values: dict[str, Any] = {
        "binance_futures_env": "testnet",
        "binance_futures_trading_api_key": SecretStr("t" * 64),
        "binance_futures_trading_api_secret": SecretStr("u" * 64),
        "binance_futures_order_rest_url": "https://demo-fapi.binance.test",
        "binance_futures_order_ws_url": "wss://demo-fstream.binance.test",
        "binance_futures_account": "binance_testnet",
        "binance_futures_max_leverage": 2,
        "binance_futures_require_isolated": True,
        "trading_enabled": True,
        "live_trading_enabled": False,
        "max_volume_lots": None,
    }
    values.update(overrides)
    return settings(**values)


class FakeBinanceTrading(FakeBinanceFutures):
    """``FakeBinanceFutures`` plus order entry, positions and user-stream events.

    Book: BTCUSDT 60000.00 / 60000.10, ETHUSDT 2500.00 / 2500.01. Market orders
    fill at the touch; limits rest (a post-only limit that would cross is
    rejected with -5022); ``fill(order_id)`` fills a resting one. Every change
    pushes the ``ORDER_TRADE_UPDATE`` / ``ACCOUNT_UPDATE`` Binance would send to
    ``user_queue`` (set by ``FakeUserStream``). ``fail_next`` makes the next
    order send fail: ``"transport"``, ``"500"`` or ``"-1007"`` (all after the
    order may have been placed, as with the real thing).
    """

    def __init__(self, now: datetime = NOW) -> None:
        super().__init__(now)
        self.account_flags: dict[str, Any] = {"dualSidePosition": False, "multiAssetsMargin": False}
        self.symbol_config: dict[str, dict[str, Any]] = {
            s: {"leverage": 2, "marginType": "ISOLATED"} for s in ("BTCUSDT", "ETHUSDT")
        }
        self.orders: dict[int, dict[str, Any]] = {}
        self.positions: dict[str, Decimal] = {"BTCUSDT": Decimal(0), "ETHUSDT": Decimal(0)}
        self.entry: dict[str, Decimal] = {}
        self.countdowns: dict[str, int] = {}
        self.listen_key_calls: list[str] = []
        self.user_queue: asyncio.Queue[dict[str, Any]] | None = None
        self.fail_next: str | None = None
        self._next_id = 1000

    # helpers ---------------------------------------------------------------------

    def _push(self, event: dict[str, Any]) -> None:
        if self.user_queue is not None:
            self.user_queue.put_nowait(event)

    def _order_event(self, order: dict[str, Any], exec_type: str, last_qty: str = "0") -> None:
        self._push(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": NOW_MS,
                "T": NOW_MS,
                "o": {
                    "s": order["symbol"],
                    "c": order["clientOrderId"],
                    "S": order["side"],
                    "o": order["type"],
                    "q": order["origQty"],
                    "p": order["price"],
                    "ap": order["avgPrice"],
                    "x": exec_type,
                    "X": order["status"],
                    "i": order["orderId"],
                    "l": last_qty,
                    "z": order["executedQty"],
                    "L": order["avgPrice"],
                    "n": "0.01",
                    "N": "USDT",
                    "T": NOW_MS,
                    "R": order["reduceOnly"],
                    "rp": "0",
                    "m": order["type"] == "LIMIT",
                },
            }
        )

    def _account_event(self, symbol: str) -> None:
        self._push(
            {
                "e": "ACCOUNT_UPDATE",
                "E": NOW_MS,
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "1000", "cw": "1000"}],
                    "P": [
                        {
                            "s": symbol,
                            "pa": str(self.positions[symbol]),
                            "ep": str(self.entry.get(symbol, 0)),
                            "ps": "BOTH",
                        }
                    ],
                },
            }
        )

    def fill(self, order_id: int) -> None:
        order = self.orders[order_id]
        qty = Decimal(order["origQty"])
        signed = qty if order["side"] == "BUY" else -qty
        self.positions[order["symbol"]] += signed
        self.entry[order["symbol"]] = Decimal(order["price"])
        order.update(status="FILLED", executedQty=order["origQty"], avgPrice=order["price"])
        self._order_event(order, "TRADE", order["origQty"])
        self._account_event(order["symbol"])

    @staticmethod
    def _error(code: int, msg: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "msg": msg})

    # endpoints -------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        params = request.url.params
        trading = (
            path.startswith("/fapi/v1/order")
            or path
            in {
                "/fapi/v1/openOrders",
                "/fapi/v1/allOpenOrders",
                "/fapi/v1/countdownCancelAll",
                "/fapi/v1/symbolConfig",
                "/fapi/v3/positionRisk",
                "/fapi/v1/listenKey",
            }
            or (path == "/fapi/v1/accountConfig")
        )
        if not trading:
            return super().handler(request)
        self.requests.append(request)
        if "X-MBX-APIKEY" not in request.headers:
            return self._error(-2014, "API-key format invalid.", 401)
        if path == "/fapi/v1/listenKey":
            self.listen_key_calls.append(method)
            return httpx.Response(200, json={"listenKey": "lk-test"} if method != "DELETE" else {})
        if "signature" not in params:
            return self._error(-1102, "Mandatory parameter 'signature' was not sent.")
        if path == "/fapi/v1/accountConfig":
            return httpx.Response(200, json={"feeTier": 0, "canTrade": True, **self.account_flags})
        if path == "/fapi/v1/symbolConfig":
            symbol = params["symbol"]
            return httpx.Response(200, json=[{"symbol": symbol, **self.symbol_config[symbol]}])
        if path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": s,
                        "positionSide": "BOTH",
                        "positionAmt": str(a),
                        "entryPrice": str(self.entry.get(s, 0)),
                    }
                    for s, a in self.positions.items()
                ],
            )
        if path == "/fapi/v1/openOrders":
            return httpx.Response(
                200,
                json=[
                    o
                    for o in self.orders.values()
                    if o["symbol"] == params["symbol"]
                    and o["status"] in {"NEW", "PARTIALLY_FILLED"}
                ],
            )
        if path == "/fapi/v1/allOpenOrders" and method == "DELETE":
            for order in self.orders.values():
                if order["symbol"] == params["symbol"] and order["status"] == "NEW":
                    order["status"] = "CANCELED"
                    self._order_event(order, "CANCELED")
            return httpx.Response(
                200, json={"code": 200, "msg": "The operation of cancel all open order is done."}
            )
        if path == "/fapi/v1/countdownCancelAll":
            self.countdowns[params["symbol"]] = int(params["countdownTime"])
            return httpx.Response(
                200, json={"symbol": params["symbol"], "countdownTime": params["countdownTime"]}
            )
        if path == "/fapi/v1/order" and method == "GET":
            order = self._find(params)
            return (
                httpx.Response(200, json=order)
                if order
                else self._error(-2013, "Order does not exist.")
            )
        if path == "/fapi/v1/order" and method == "DELETE":
            order = self._find(params)
            if order is None or order["status"] != "NEW":
                return self._error(-2011, "Unknown order sent.")
            order["status"] = "CANCELED"
            self._order_event(order, "CANCELED")
            return httpx.Response(200, json=order)
        if path == "/fapi/v1/order" and method == "POST":
            return self._place(params)
        return httpx.Response(404, json={"code": -1, "msg": path})

    def _find(self, params: Any) -> dict[str, Any] | None:
        if "orderId" in params:
            return self.orders.get(int(params["orderId"]))
        coid = params.get("origClientOrderId")
        return next((o for o in self.orders.values() if o["clientOrderId"] == coid), None)

    def _place(self, params: Any) -> httpx.Response:
        coid = params["newClientOrderId"]
        if any(o["clientOrderId"] == coid for o in self.orders.values()):
            return self._error(-4116, "ClientOrderId is duplicated.")
        symbol, side, kind = params["symbol"], params["side"], params["type"]
        book = BOOK[symbol]
        reduce_only = params.get("reduceOnly") == "true"
        position = self.positions[symbol]
        if reduce_only and (position == 0 or (side == "BUY") == (position > 0)):
            return self._error(-2022, "ReduceOnly Order is rejected.")
        price = params.get("price", "0")
        if params.get("timeInForce") == "GTX":
            crosses = (side == "BUY" and Decimal(price) >= Decimal(book["ask"])) or (
                side == "SELL" and Decimal(price) <= Decimal(book["bid"])
            )
            if crosses:
                return self._error(
                    -5022,
                    "Post Only order will be rejected: it would execute as taker.",
                )
        self._next_id += 1
        order = {
            "orderId": self._next_id,
            "symbol": symbol,
            "clientOrderId": coid,
            "side": side,
            "type": kind,
            "origQty": params["quantity"],
            "price": price,
            "executedQty": "0",
            "avgPrice": "0",
            "status": "NEW",
            "reduceOnly": reduce_only,
            "timeInForce": params.get("timeInForce", ""),
        }
        self.orders[order["orderId"]] = order
        failure, self.fail_next = self.fail_next, None
        if kind == "MARKET":
            fill_price = book["ask"] if side == "BUY" else book["bid"]
            order["price"] = fill_price
            self.fill(order["orderId"])
            order["avgPrice"] = fill_price
        else:
            self._order_event(order, "NEW")
        if failure == "transport":
            raise httpx.ReadTimeout("timed out after the order was placed")
        if failure == "500":
            return httpx.Response(503, text="Service Unavailable")
        if failure == "-1007":
            return self._error(
                -1007,
                "Timeout waiting for response from backend server; execution status unknown.",
            )
        return httpx.Response(200, json=order)


class FakeUserStream:
    """A ``ws_connect`` for the user-data stream, fed by ``FakeBinanceTrading``."""

    def __init__(self, fake: FakeBinanceTrading) -> None:
        self.fake = fake
        self.urls: list[str] = []
        self.drop = asyncio.Event()  # set to drop the current connection

    def __call__(self, url: str) -> contextlib.AbstractAsyncContextManager[AsyncIterator[str]]:
        self.urls.append(url)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.fake.user_queue = queue
        self.drop = asyncio.Event()
        drop = self.drop

        @contextlib.asynccontextmanager
        async def connect() -> AsyncIterator[AsyncIterator[str]]:
            async def frames() -> AsyncIterator[str]:
                # Polling, not tasks: nothing is left pending when the
                # connection is cancelled at shutdown.
                while not drop.is_set():
                    try:
                        event = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.001)
                        continue
                    yield json.dumps(event)

            yield frames()

        return connect()
