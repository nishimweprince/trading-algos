"""Binance USDⓈ-M order entry as a ``ta_plugin_api.ExecutionProvider``.

One account (``BINANCE_FUTURES_ACCOUNT_ALIAS``), one environment
(``BINANCE_FUTURES_ENV``: ``testnet`` is Binance demo trading, ``mainnet`` is
real money and also needs ``LIVE_TRADING_ENABLED`` on the gateway).

How the platform contract maps onto Binance:

- **Orders.** ``OrderRequest`` → ``POST /fapi/v1/order`` with
  ``newClientOrderId = operation_id.hex``. ``volume_lots`` is the quantity in
  the base asset (BTC, ETH), checked against LOT_SIZE / MARKET_LOT_SIZE and
  MIN_NOTIONAL before anything is reserved. ``post_only`` → ``timeInForce=GTX``,
  ``reduce_only`` → ``reduceOnly``. Stops are ``STOP_MARKET`` at ``entry_price``.
  Binance has no stop loss / take profit attached to an order: exits are their
  own reduce-only orders, and a request carrying them is refused (422).
- **Positions.** Binance has no position id; in one-way mode a position is a
  symbol. ``positions()`` reports a stable id per symbol (crc32), and
  ``CLOSE_POSITION`` with that id becomes a reduce-only market order.
- **Outcomes.** A 4xx with a Binance code is REJECTED (nothing was placed); no
  response, a 5xx, or ``-1007`` (backend timeout) is UNKNOWN and is reconciled
  by client order id, never resubmitted. The user-data stream settles targets
  as orders fill or cancel; states only ever move forward.
- **Safety.** ``AccountControlVenue``: cancel-all, reduce-only flatten, and the
  exchange-side dead-man countdown (``countdownCancelAll``).
- **Preflight** (verify, never change account settings): one-way mode,
  single-asset margin, leverage ≤ ``BINANCE_FUTURES_MAX_LEVERAGE`` and isolated
  margin per symbol. Until it passes the provider is not ready and refuses
  orders, with the exact fix in ``readiness()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from ta_contracts import (
    BrokerOrder,
    BrokerPosition,
    Direction,
    ExecutionType,
    OperationAction,
    TargetState,
    TimeInForce,
)
from ta_core import ServiceError
from ta_core.logging_config import log_event
from ta_plugin_api import ControlResult, LedgerPort, TargetOutcome

from .instruments import OrderFilters, load_filters
from .rest import FapiRest, VenueNotSent, VenueTransportError
from .streams import WsConnect
from .user_stream import UserStream

__all__ = ["BinanceFuturesExecution", "position_id_for"]

PROVIDER = "binance_futures"
BOOT_RETRY_SECONDS = 30.0
UNKNOWN_CODES = {-1007, -1006}  # backend timeout / unexpected response: may have executed
FINAL = {
    TargetState.FILLED,
    TargetState.CLOSED,
    TargetState.CANCELLED,
    TargetState.REJECTED,
    TargetState.PARTIALLY_FILLED_FINAL,
}
RANK = {
    TargetState.ACCEPTED: 1,
    TargetState.PLACED: 1,
    TargetState.PARTIALLY_FILLED: 2,
    **{state: 3 for state in FINAL},
}
W_ORDER, W_QUERY, W_OPEN, W_CONFIG, W_POSITIONS, W_CANCEL_ALL, W_COUNTDOWN = 1, 1, 1, 5, 5, 1, 10


def position_id_for(symbol: str) -> int:
    """A stable positive id for a one-way position (Binance has none)."""
    return zlib.crc32(symbol.encode()) or 1


def _text(value: Decimal) -> str:
    """Decimal without exponent or trailing zeros, as Binance wants it."""
    return format(value.normalize(), "f")


def _multiple(value: Decimal, step: Decimal) -> bool:
    try:
        return step <= 0 or (value / step) % 1 == 0
    except InvalidOperation:
        return False


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


@dataclass(frozen=True)
class _Order:
    """One Binance order, from REST or the user stream, in one shape."""

    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    type: str
    orig_qty: Decimal
    cum_qty: Decimal
    avg_price: Decimal
    price: Decimal
    reduce_only: bool

    @classmethod
    def from_rest(cls, row: dict[str, Any]) -> _Order:
        return cls(
            symbol=row["symbol"],
            order_id=int(row["orderId"]),
            client_order_id=str(row.get("clientOrderId", "")),
            status=str(row.get("status", "")),
            side=str(row.get("side", "")),
            type=str(row.get("type", "")),
            orig_qty=_decimal(row.get("origQty", 0)),
            cum_qty=_decimal(row.get("executedQty", 0)),
            avg_price=_decimal(row.get("avgPrice", 0)),
            price=_decimal(row.get("price", 0)),
            reduce_only=bool(row.get("reduceOnly", False)),
        )

    @classmethod
    def from_stream(cls, o: dict[str, Any]) -> _Order:
        return cls(
            symbol=o["s"],
            order_id=int(o["i"]),
            client_order_id=str(o.get("c", "")),
            status=str(o.get("X", "")),
            side=str(o.get("S", "")),
            type=str(o.get("o", "")),
            orig_qty=_decimal(o.get("q", 0)),
            cum_qty=_decimal(o.get("z", 0)),
            avg_price=_decimal(o.get("ap", 0)),
            price=_decimal(o.get("p", 0)),
            reduce_only=bool(o.get("R", False)),
        )

    @property
    def open(self) -> bool:
        return self.status in {"NEW", "PARTIALLY_FILLED"}


class BinanceFuturesExecution:
    name = PROVIDER

    def __init__(
        self,
        settings: Any,
        *,
        rest: FapiRest | None = None,
        ws_connect: WsConnect | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self.settings = settings
        self.account: str = settings.binance_futures_account
        self.symbols: tuple[str, ...] = settings.binance_futures_symbols
        self.environment: str = settings.binance_futures_env
        self.is_live = self.environment == "mainnet"
        self._rest = rest or FapiRest(
            settings,
            base_url=settings.binance_futures_order_rest_url,
            api_key=settings.binance_futures_trading_api_key,
            api_secret=settings.binance_futures_trading_api_secret,
        )
        self._owns_rest = rest is None
        self._sleep = sleep
        self._clock_ms = clock_ms
        self.user_stream = UserStream(
            self._rest,
            settings.binance_futures_order_ws_url,
            self._on_event,
            on_connected=self._resync,
            ws_connect=ws_connect,
            sleep=sleep,
        )
        self.filters: dict[str, OrderFilters] = {}
        self.preflight_errors: list[str] = ["not checked yet"]
        self.reconciled = False
        self.last_error: str | None = None
        self.wallet_balance: Decimal | None = None
        self._orders: dict[int, _Order] = {}
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (amt, entry)
        self._close_actions: set[str] = set()  # client ids of CLOSE_POSITION orders
        self._seen: dict[str, TargetState] = {}  # client id -> furthest state from the stream
        self._inflight: set[str] = set()  # client ids whose send has not returned yet
        self._ledger: LedgerPort | None = None
        self._ready = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._boot(), name="binance-futures-exec-boot"))

    async def _boot(self) -> None:
        """Filters and preflight (retrying until they pass), then the user stream."""
        while True:
            try:
                self.filters = await load_filters(self._rest, self.symbols)
                self.preflight_errors = await self.preflight()
            except (ServiceError, VenueNotSent, VenueTransportError) as exc:
                self.preflight_errors = [f"preflight could not run: {exc}"]
            if not self.preflight_errors:
                break
            log_event(
                "binance_futures_preflight_failed",
                level=logging.ERROR,
                errors=self.preflight_errors,
            )
            await self._sleep(BOOT_RETRY_SECONDS)
        self._tasks.append(asyncio.create_task(self.user_stream.run(), name="binance-futures-user"))

    async def wait_ready(self, timeout_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout_seconds)
        return self._ready.is_set()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self.user_stream.close()
        if self._owns_rest:
            await self._rest.aclose()

    @property
    def ready(self) -> bool:
        return (
            not self.preflight_errors
            and self.user_stream.connected
            and self.reconciled
            and bool(self.filters)
        )

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        details: dict[str, Any] = {
            "environment": self.environment,
            "account": self.account,
            "preflight": self.preflight_errors or "ok",
            "user_stream": self.user_stream.connected,
            "user_stream_reconnects": self.user_stream.reconnects,
            "reconciled": self.reconciled,
            "open_orders": len(self._orders),
            "positions": {s: str(a) for s, (a, _) in self._positions.items() if a},
        }
        if self.last_error or self.user_stream.last_error:
            details["last_error"] = self.last_error or self.user_stream.last_error
        return self.ready, details

    def attach_ledger(self, ledger: LedgerPort) -> None:
        self._ledger = ledger

    # --- REST helpers ---------------------------------------------------------------

    async def _call(self, method: str, path: str, params: dict[str, Any], weight: int) -> Any:
        """A signed call that must succeed (not order dispatch): ServiceError otherwise."""
        try:
            response = await self._rest.raw(method, path, params, weight)
        except (VenueNotSent, VenueTransportError) as exc:
            raise ServiceError(503, "broker_unavailable", str(exc)) from None
        body = response.json() if response.content else {}
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg") if isinstance(body, dict) else None
            raise ServiceError(
                503, "broker_error", f"{method} {path}: {code} {msg}", {"code": code}
            )
        return body

    async def preflight(self) -> list[str]:
        """What must change on the account before this adapter trades on it."""
        errors: list[str] = []
        config = await self._call("GET", "/fapi/v1/accountConfig", {}, W_CONFIG)
        if config.get("dualSidePosition"):
            errors.append("Hedge mode is on: switch Position Mode to One-way")
        if config.get("multiAssetsMargin"):
            errors.append("Multi-Assets mode is on: switch to Single-Asset margin")
        max_leverage = self.settings.binance_futures_max_leverage
        for symbol in self.symbols:
            rows = await self._call("GET", "/fapi/v1/symbolConfig", {"symbol": symbol}, W_CONFIG)
            row = rows[0] if isinstance(rows, list) and rows else rows
            leverage = int(row.get("leverage", 0)) if isinstance(row, dict) else 0
            margin = str(row.get("marginType", "")).upper() if isinstance(row, dict) else ""
            if leverage > max_leverage:
                errors.append(
                    f"{symbol} leverage is {leverage}x: set it to {max_leverage}x or less"
                )
            if self.settings.binance_futures_require_isolated and margin not in {
                "ISOLATED",
                "ISOLATED_MARGIN",
            }:
                errors.append(f"{symbol} margin is {margin or 'unknown'}: switch it to Isolated")
        return errors

    # --- resync, reconcile ----------------------------------------------------------

    async def _resync(self) -> None:
        """After every user-stream (re)connect: orders, positions, then the ledger."""
        self.reconciled = False
        previous = dict(self._orders)
        fresh: dict[int, _Order] = {}
        for symbol in self.symbols:
            for row in await self._call("GET", "/fapi/v1/openOrders", {"symbol": symbol}, W_OPEN):
                order = _Order.from_rest(row)
                fresh[order.order_id] = order
        positions = await self._call("GET", "/fapi/v3/positionRisk", {}, W_POSITIONS)
        self._positions = {
            row["symbol"]: (_decimal(row.get("positionAmt")), _decimal(row.get("entryPrice")))
            for row in positions
            if row.get("symbol") in self.symbols
        }
        self._orders = fresh
        # Orders that left the book while the stream was down: learn how.
        for order_id, order in previous.items():
            if order_id not in fresh:
                row = await self._query(order.symbol, order_id=order_id)
                if row is not None:
                    self._settle(_Order.from_rest(row), source="resync")
        await self.reconcile_unknown(include_dispatched=True)
        self.reconciled = True
        self._ready.set()

    async def _query(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = client_order_id
        try:
            response = await self._rest.raw("GET", "/fapi/v1/order", params, W_QUERY)
        except (VenueNotSent, VenueTransportError):
            return None
        if response.status_code >= 400:
            return None  # -2013 order does not exist, or transient
        return response.json()

    async def reconcile(self) -> None:
        """At startup: RESERVED never left; everything else is asked of Binance."""
        if self._ledger is None:
            return
        for target in self._ledger.unresolved_targets([self.account]):
            if target.state is TargetState.RESERVED:
                self._ledger.update_target(
                    target.operation_id,
                    target.account,
                    TargetState.REJECTED,
                    error_code="restart_before_execution",
                    error_message="the gateway restarted before this order was sent",
                )
        await self.reconcile_unknown(include_dispatched=True)

    async def reconcile_unknown(self, *, include_dispatched: bool = False) -> None:
        """UNKNOWN (and, on restart/resync, DISPATCHED/ACCEPTED) targets by client id.

        Found → its real state. Not found → stays UNKNOWN (DISPATCHED becomes
        UNKNOWN): never flipped to rejected, because "not found yet" is not
        "never sent", and a resubmit could double the position.
        """
        if self._ledger is None:
            return
        wanted = {TargetState.UNKNOWN}
        if include_dispatched:
            wanted |= {TargetState.DISPATCHED, TargetState.ACCEPTED}
        for target in self._ledger.unresolved_targets([self.account]):
            if target.state not in wanted or not target.client_order_id:
                continue
            if target.client_order_id in self._inflight:
                continue  # its own dispatch is still waiting for the answer
            found = None
            for symbol in self.symbols:
                found = await self._query(symbol, client_order_id=target.client_order_id)
                if found is not None:
                    break
            if found is not None:
                self._settle(_Order.from_rest(found), source="reconcile")
            elif target.state is TargetState.DISPATCHED:
                self._ledger.update_target(
                    target.operation_id,
                    target.account,
                    TargetState.UNKNOWN,
                    error_code="execution_outcome_unknown",
                    error_message="not found at Binance yet; will be checked again",
                )

    # --- the user stream -------------------------------------------------------------

    def _on_event(self, data: dict[str, Any]) -> None:
        kind = data.get("e")
        if kind == "ORDER_TRADE_UPDATE" and isinstance(data.get("o"), dict):
            order = _Order.from_stream(data["o"])
            if order.symbol not in self.symbols:
                return
            self._settle(order, source="stream", raw=data["o"])
        elif kind == "ACCOUNT_UPDATE" and isinstance(data.get("a"), dict):
            update = data["a"]
            for row in update.get("P", []):
                if row.get("s") in self.symbols and row.get("ps", "BOTH") == "BOTH":
                    self._positions[row["s"]] = (_decimal(row.get("pa")), _decimal(row.get("ep")))
            for row in update.get("B", []):
                if row.get("a") == "USDT":
                    self.wallet_balance = _decimal(row.get("wb"))

    def _state_for(self, order: _Order) -> tuple[TargetState, dict[str, Any]]:
        close = order.client_order_id in self._close_actions
        if order.status == "NEW":
            return (TargetState.ACCEPTED if order.type == "MARKET" else TargetState.PLACED), {}
        if order.status == "PARTIALLY_FILLED":
            return TargetState.PARTIALLY_FILLED, {}
        if order.status == "FILLED":
            return (TargetState.CLOSED if close else TargetState.FILLED), {}
        if order.status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            if order.cum_qty > 0:
                return TargetState.PARTIALLY_FILLED_FINAL, {}
            if order.status == "CANCELED":
                return TargetState.CANCELLED, {}
            code = "self_trade_prevented" if order.status == "EXPIRED_IN_MATCH" else "order_expired"
            return TargetState.REJECTED, {"error_code": code, "error_message": order.status}
        return TargetState.UNKNOWN, {
            "error_code": "unexpected_status",
            "error_message": order.status,
        }

    def _values(self, order: _Order, extra: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {"order_id": order.order_id, **extra}
        if order.cum_qty > 0:
            values["executed_volume_lots"] = order.cum_qty
            if order.avg_price > 0:
                values["execution_price"] = order.avg_price
        return values

    def _settle(self, order: _Order, *, source: str, raw: dict[str, Any] | None = None) -> None:
        """Bring the cache and the ledger target in line with this order's state."""
        if order.open:
            self._orders[order.order_id] = order
        else:
            self._orders.pop(order.order_id, None)
        state, extra = self._state_for(order)
        previous = self._seen.get(order.client_order_id)
        if previous is None or RANK.get(state, 0) >= RANK.get(previous, 0):
            self._seen[order.client_order_id] = state
        if self._ledger is None or not order.client_order_id:
            return
        hit = self._ledger.find_by_client_order_id(order.client_order_id)
        if hit is None:
            return  # not ours through the gateway (flatten orders, manual orders)
        operation_id, account = hit
        if raw is not None:
            self._ledger.append_event(
                account=account,
                event_type="binance_order_update",
                operation_id=operation_id,
                payload={
                    key: raw.get(key)
                    for key in (
                        "s",
                        "c",
                        "i",
                        "S",
                        "o",
                        "x",
                        "X",
                        "l",
                        "z",
                        "L",
                        "ap",
                        "n",
                        "N",
                        "rp",
                        "T",
                        "t",
                        "R",
                        "m",
                    )
                },
            )
        current = self._target_state(operation_id, account)
        if current in FINAL or RANK.get(state, 0) < RANK.get(current, 0):
            return  # never move a target backwards (late NEW after FILLED, etc.)
        self._ledger.update_target(
            operation_id,
            account,
            state,
            details={"binance_status": order.status, "settled_by": source},
            **self._values(order, extra),
        )

    def _target_state(self, operation_id: str, account: str) -> TargetState | None:
        assert self._ledger is not None
        response = self._ledger.get(operation_id)
        if response is None:
            return None
        for target in response.targets:
            if target.account == account:
                return target.state
        return None

    # --- platform reads -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return (self.account,)

    def _require(self, account: str) -> None:
        if account != self.account:
            raise KeyError(account)

    def account_statuses(self) -> list[dict[str, Any]]:
        entry = self._entry_enabled()
        return [
            {
                "alias": self.account,
                "provider": self.name,
                "ctid_trader_account_id": None,
                "environment": self.environment,
                "is_live": self.is_live,
                "connected": self.user_stream.connected,
                "reconciled": self.reconciled,
                "broker_access_rights": "trade" if not self.preflight_errors else None,
                "available_for_trading": self.ready,
                "order_entry_enabled": entry,
                "position_close_enabled": entry,
                "wallet_balance_usdt": None
                if self.wallet_balance is None
                else str(self.wallet_balance),
            }
        ]

    def _entry_enabled(self) -> bool:
        trading = bool(getattr(self.settings, "trading_enabled", False))
        live_ok = not self.is_live or bool(getattr(self.settings, "live_trading_enabled", False))
        return self.ready and trading and live_ok

    def orders(self, account: str) -> list[BrokerOrder]:
        self._require(account)
        return [
            BrokerOrder(
                account=account,
                order_id=order.order_id,
                position_id=position_id_for(order.symbol),
                client_order_id=order.client_order_id,
                instrument=order.symbol,
                volume_lots=order.orig_qty - order.cum_qty,
                state=order.status.lower(),
            )
            for order in self._orders.values()
        ]

    def positions(self, account: str) -> list[BrokerPosition]:
        self._require(account)
        return [
            BrokerPosition(
                account=account,
                position_id=position_id_for(symbol),
                instrument=symbol,
                volume_lots=abs(amount),
                direction=Direction.BUY if amount > 0 else Direction.SELL,
                price=entry or None,
            )
            for symbol, (amount, entry) in self._positions.items()
            if amount != 0
        ]

    def client_order_id(self, operation_id: UUID, account: str) -> str:
        # 32 characters: Binance keeps up to 36 ([.A-Z:/a-z0-9_-]).
        return operation_id.hex

    # --- prepare ----------------------------------------------------------------------

    def _gate(self, account: str) -> None:
        if account != self.account:
            raise ServiceError(422, "account_not_allowed", f"{account} is not served here")
        if not self.ready:
            raise ServiceError(
                503, "account_not_ready", "Binance account not ready", self.readiness()[1]
            )
        if not getattr(self.settings, "trading_enabled", False):
            raise ServiceError(503, "trading_disabled", "TRADING_ENABLED is false")
        if self.is_live and not getattr(self.settings, "live_trading_enabled", False):
            raise ServiceError(
                503, "live_trading_disabled", "mainnet account and LIVE_TRADING_ENABLED is false"
            )

    def _quantity(self, symbol: str, lots: Decimal, *, market: bool) -> Decimal:
        f = self.filters[symbol]
        cap = getattr(self.settings, "max_volume_lots", None)
        if cap is not None and lots > cap:
            raise ServiceError(422, "volume_exceeds_limit", f"{lots} > MAX_VOLUME_LOTS {cap}")
        step = f.market_lot_step if market else f.lot_step
        low, high = (f.market_min_qty, f.market_max_qty) if market else (f.min_qty, f.max_qty)
        if not _multiple(lots, step):
            raise ServiceError(422, "invalid_volume_step", f"{lots} is not a multiple of {step}")
        if lots < low or lots > high:
            raise ServiceError(422, "invalid_volume", f"{lots} outside [{low}, {high}]")
        return lots

    async def prepare(
        self, action: OperationAction, request: Any, target: Any, client_order_id: str
    ) -> dict[str, Any]:
        self._gate(target.account)
        if action is OperationAction.PLACE_ORDER:
            return self._prepare_place(request, target, client_order_id)
        if action is OperationAction.CANCEL_ORDER:
            order = self._orders.get(int(target.order_id))
            if order is None:
                raise ServiceError(422, "order_not_found", f"no open order {target.order_id}")
            return {
                "method": "DELETE",
                "path": "/fapi/v1/order",
                "params": {"symbol": order.symbol, "orderId": order.order_id},
                "kind": "cancel",
            }
        if action is OperationAction.CLOSE_POSITION:
            return self._prepare_close(target, client_order_id)
        raise ServiceError(
            501,
            "action_not_supported",
            f"{action.value} is not supported on Binance futures; cancel and place instead",
        )

    def _prepare_place(self, request: Any, target: Any, client_order_id: str) -> dict[str, Any]:
        symbol = request.instrument
        if symbol not in self.filters:
            raise ServiceError(422, "instrument_not_available", f"{symbol} is not configured")
        if any(
            getattr(request, name, None) is not None
            for name in ("stop_loss", "take_profit", "stop_loss_distance", "take_profit_distance")
        ):
            raise ServiceError(
                422,
                "protection_not_supported",
                "Binance futures orders carry no SL/TP: send exits as reduce_only orders",
            )
        f = self.filters[symbol]
        kind = request.execution_type
        quantity = self._quantity(symbol, target.volume_lots, market=kind is ExecutionType.MARKET)
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": "BUY" if request.direction is Direction.BUY else "SELL",
            "quantity": _text(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if kind is ExecutionType.MARKET:
            params["type"] = "MARKET"
        else:
            price = request.entry_price
            if not _multiple(price, f.tick_size):
                raise ServiceError(
                    422, "invalid_price_step", f"{price} is not a multiple of {f.tick_size}"
                )
            if kind is ExecutionType.LIMIT:
                if f.min_notional and price * quantity < f.min_notional:
                    raise ServiceError(
                        422, "notional_too_small", f"{price * quantity} < {f.min_notional}"
                    )
                params.update(type="LIMIT", price=_text(price))
            else:
                params.update(type="STOP_MARKET", stopPrice=_text(price))
            if request.post_only:
                if request.time_in_force is TimeInForce.GTD:
                    raise ServiceError(422, "invalid_time_in_force", "post_only cannot be GTD")
                params["timeInForce"] = "GTX"
            elif request.time_in_force is TimeInForce.GTD:
                params["timeInForce"] = "GTD"
                params["goodTillDate"] = int(request.expires_at.timestamp() * 1000)
            elif kind is ExecutionType.LIMIT:
                params["timeInForce"] = "GTC"
        if request.reduce_only:
            params["reduceOnly"] = "true"
        return {"method": "POST", "path": "/fapi/v1/order", "params": params, "kind": "place"}

    def _prepare_close(self, target: Any, client_order_id: str) -> dict[str, Any]:
        symbol = next((s for s in self.symbols if position_id_for(s) == target.position_id), None)
        amount = self._positions.get(symbol or "", (Decimal(0), Decimal(0)))[0]
        if symbol is None or amount == 0:
            raise ServiceError(422, "position_not_found", f"no open position {target.position_id}")
        if target.volume_lots > abs(amount):
            raise ServiceError(
                422, "close_volume_too_large", f"{target.volume_lots} > {abs(amount)}"
            )
        quantity = self._quantity(symbol, target.volume_lots, market=True)
        self._close_actions.add(client_order_id)
        return {
            "method": "POST",
            "path": "/fapi/v1/order",
            "params": {
                "symbol": symbol,
                "side": "SELL" if amount > 0 else "BUY",
                "type": "MARKET",
                "quantity": _text(quantity),
                "reduceOnly": "true",
                "newClientOrderId": client_order_id,
                "newOrderRespType": "RESULT",
            },
            "kind": "close",
        }

    # --- dispatch ---------------------------------------------------------------------

    async def dispatch(
        self,
        operation_id: UUID,
        account: str,
        action: OperationAction,
        prepared: dict[str, Any],
        client_order_id: str,
    ) -> TargetOutcome:
        """Send once. Never raises; never resends."""
        request = {"method": prepared["method"], "path": prepared["path"]}
        self._inflight.add(client_order_id)
        try:
            response = await self._rest.raw(
                prepared["method"], prepared["path"], prepared["params"], W_ORDER
            )
        except VenueNotSent as exc:
            return TargetOutcome(
                TargetState.REJECTED,
                {"error_code": "not_sent", "error_message": str(exc)},
                {"request": request},
            )
        except VenueTransportError as exc:
            return TargetOutcome(
                TargetState.UNKNOWN,
                {"error_code": "execution_outcome_unknown", "error_message": str(exc)},
                {"request": request},
            )
        finally:
            self._inflight.discard(client_order_id)
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 500:
            return TargetOutcome(
                TargetState.UNKNOWN,
                {
                    "error_code": "execution_outcome_unknown",
                    "error_message": f"HTTP {response.status_code}",
                },
                {"request": request},
            )
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            msg = str(body.get("msg", "")) if isinstance(body, dict) else ""
            if code in UNKNOWN_CODES:
                return TargetOutcome(
                    TargetState.UNKNOWN,
                    {"error_code": "execution_outcome_unknown", "error_message": f"{code} {msg}"},
                    {"request": request, "binance_code": code},
                )
            error_code = {
                -5022: "post_only_would_take",
                -2022: "reduce_only_rejected",
                -2019: "insufficient_margin",
                -2011: "order_not_found",
                -4164: "notional_too_small",
            }.get(code, f"binance_{code}" if code is not None else f"http_{response.status_code}")
            return TargetOutcome(
                TargetState.REJECTED,
                {"error_code": error_code, "error_message": msg[:200]},
                {"request": request, "binance_code": code},
            )
        order = _Order.from_rest(body)
        if prepared["kind"] == "cancel":
            self._orders.pop(order.order_id, None)
            return TargetOutcome(
                TargetState.CANCELLED,
                {"order_id": order.order_id},
                {"binance_status": order.status},
            )
        if order.open:
            self._orders[order.order_id] = order
        state, extra = self._state_for(order)
        # The stream may already have reported a later state for this order.
        streamed = self._seen.get(client_order_id)
        if streamed is not None and RANK.get(streamed, 0) > RANK.get(state, 0):
            state = streamed
        return TargetOutcome(
            state,
            self._values(order, extra),
            {"binance_status": order.status, "request": request},
        )

    # --- account controls (AccountControlVenue) ---------------------------------------

    def _symbols_for(self, account: str, instrument: str | None) -> list[str]:
        self._require(account)
        if instrument is None:
            return list(self.symbols)
        symbol = instrument.upper()
        if symbol not in self.symbols:
            raise KeyError(instrument)
        return [symbol]

    async def _control_call(
        self, method: str, path: str, params: dict[str, Any], weight: int
    ) -> dict[str, Any]:
        try:
            response = await self._rest.raw(method, path, params, weight)
        except (VenueNotSent, VenueTransportError) as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        return {"ok": response.status_code < 400, "status": response.status_code, "body": body}

    async def cancel_all(self, account: str, instrument: str | None = None) -> ControlResult:
        results = {
            symbol: await self._control_call(
                "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, W_CANCEL_ALL
            )
            for symbol in self._symbols_for(account, instrument)
        }
        return ControlResult(all(r["ok"] for r in results.values()), {"symbols": results})

    async def flatten(self, account: str, instrument: str | None = None) -> ControlResult:
        symbols = self._symbols_for(account, instrument)
        # Fresh positions, not the cache: this is the kill path.
        risk = await self._control_call("GET", "/fapi/v3/positionRisk", {}, W_POSITIONS)
        if not risk["ok"] or not isinstance(risk.get("body"), list):
            return ControlResult(False, {"positions": risk})
        results: dict[str, Any] = {}
        for row in risk["body"]:
            symbol = row.get("symbol")
            amount = _decimal(row.get("positionAmt"))
            if symbol not in symbols or amount == 0:
                continue
            results[symbol] = await self._control_call(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": "SELL" if amount > 0 else "BUY",
                    "type": "MARKET",
                    "quantity": _text(abs(amount)),
                    "reduceOnly": "true",
                    "newClientOrderId": f"flat-{symbol.lower()}-{self._clock_ms()}"[:36],
                    "newOrderRespType": "RESULT",
                },
                W_ORDER,
            )
        return ControlResult(all(r["ok"] for r in results.values()), {"orders": results})

    async def dead_man(
        self, account: str, instruments: Sequence[str], countdown_ms: int
    ) -> ControlResult:
        self._require(account)
        symbols = [s.upper() for s in instruments] or list(self.symbols)
        unknown = [s for s in symbols if s not in self.symbols]
        if unknown:
            raise KeyError(unknown[0])
        results = {
            symbol: await self._control_call(
                "POST",
                "/fapi/v1/countdownCancelAll",
                {"symbol": symbol, "countdownTime": max(0, int(countdown_ms))},
                W_COUNTDOWN,
            )
            for symbol in symbols
        }
        return ControlResult(all(r["ok"] for r in results.values()), {"symbols": results})
