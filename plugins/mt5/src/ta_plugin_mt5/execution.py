"""MetaTrader 5 execution: one terminal, one account alias.

Everything here is broker policy that used to live in execution-service's MT5
signal service: symbol selection, the stops-level and spread rules for
protective levels, volume steps, the filling policy, the request shape,
result normalization and history matching. The service keeps idempotency,
freshness and notification; this module decides what the terminal is sent.

Every terminal call blocks and the MetaTrader5 package keeps global state, so
all of them run in a thread under one asyncio lock. ``exclusive`` exposes that
lock to callers that sequence several calls themselves (the legacy signal
path and OCO); ``dispatch`` takes it on its own.

The account alias is the process profile (``hfm``, ``ftmo``, ``deriv``): one
process attaches to exactly one terminal, so it serves exactly one account.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

from ta_contracts import (
    AmendOrderTarget,
    BrokerOrder,
    BrokerPosition,
    ClosePositionTarget,
    Direction,
    ExecutionType,
    OperationAction,
    OrderReferenceTarget,
    OrderRequest,
    OrderTarget,
    PositionProtectionTarget,
    TargetState,
)
from ta_core import ServiceError
from ta_plugin_api.execution import LedgerPort, TargetOutcome

from .terminal import ConnectionSnapshot, MT5Adapter, SymbolSnapshot, TickSnapshot

__all__ = [
    "LogFn",
    "MT5Execution",
    "MT5ExecutionSettings",
    "PlaceIntent",
    "PreparedRequest",
]

LogFn = Callable[..., None]


class MT5ExecutionSettings(Protocol):
    profile: str | None
    login: int | None
    magic_number: int
    default_deviation_points: int
    maximum_deviation_points: int
    trading_enabled: bool
    live_trading_enabled: bool

    @property
    def allowed_symbols(self) -> frozenset[str]: ...

    @property
    def maximum_volume(self) -> Decimal | None: ...


@dataclass(frozen=True)
class PlaceIntent:
    """One new order, in the terms the terminal validates.

    ``log_fields`` identify the caller's record in every event this module
    logs (``signal_id`` for the legacy signal path, ``operation_id`` for
    ``/v1/orders``).
    """

    symbol: str
    direction: Direction
    execution_type: ExecutionType
    volume: Decimal
    broker_tag: str
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    stop_loss_distance: Decimal | None = None
    take_profit_distance: Decimal | None = None
    deviation_points: int | None = None
    expires_at: datetime | None = None
    log_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SymbolContext:
    symbol: SymbolSnapshot
    tick: TickSnapshot
    entry: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    adjustments: dict[str, Any]


@dataclass(frozen=True)
class PreparedRequest:
    """A built terminal request, plus how to read its result.

    ``preflight`` is False for requests ``order_check`` cannot evaluate
    (pending-order removal and modification), which go straight to send.
    """

    request: dict[str, Any]
    success_state: TargetState
    preflight: bool = True


# ACCOUNT_TRADE_MODE_DEMO, _CONTEST and _REAL.
_TRADE_MODES = {0: "demo", 1: "contest", 2: "live"}

# UNKNOWN targets older than this are left to an operator: the periodic sweep
# should not query an ever-growing history window.
UNKNOWN_RECONCILE_WINDOW = timedelta(days=7)


def _noop_log(_event: str, **_fields: Any) -> None:
    return None


def positive_int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def broker_details(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "retcode": result.get("retcode"),
        "comment": result.get("comment"),
        "retcode_external": result.get("retcode_external"),
    }


def quantize_price(price: Decimal | None, digits: int) -> Decimal | None:
    if price is None:
        return None
    return price.quantize(Decimal(1).scaleb(-digits))


class MT5Execution:
    name = "mt5"

    def __init__(
        self,
        adapter: MT5Adapter,
        settings: MT5ExecutionSettings,
        *,
        log: LogFn | None = None,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.account = settings.profile or "mt5"
        self._log = log or _noop_log
        self._lock = asyncio.Lock()
        self._ledger: LedgerPort | None = None
        self.initialized = False

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Attach to the terminal. The caller logs and survives a failure:
        a host must not crash-loop because the terminal is still starting."""
        self.initialized = await asyncio.to_thread(self.adapter.initialize, self.settings)

    async def wait_ready(self, timeout_seconds: float) -> bool:  # noqa: ARG002
        return self.initialized

    async def close(self) -> None:
        if self.initialized:
            await asyncio.to_thread(self.adapter.shutdown)
            self.initialized = False

    def attach_ledger(self, ledger: LedgerPort) -> None:
        self._ledger = ledger

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Hold the terminal. Not re-entrant: never call ``dispatch`` inside."""
        async with self._lock:
            yield

    def connection_ready(self, connection: ConnectionSnapshot) -> bool:
        return bool(
            connection.connected
            and connection.login == self.settings.login
            and connection.trade_allowed
            and connection.expert_allowed
        )

    def connection_details(self, connection: ConnectionSnapshot) -> dict[str, Any]:
        return {
            "terminal_connected": connection.connected,
            "account_matches": connection.login == self.settings.login,
            "trade_allowed": connection.trade_allowed,
            "expert_allowed": connection.expert_allowed,
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        try:
            connection = self.adapter.connection_snapshot()
        except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
            return False, {"terminal_connected": False, "reason": type(exc).__name__}
        details = self.connection_details(connection)
        details["environment"] = self.environment()
        return self.connection_ready(connection), details

    def environment(self) -> str:
        """The account's kind, from the terminal: demo, contest, live or unknown.

        ``unknown`` (terminal down, field missing) is treated as live wherever
        it gates anything, so a failure to read it never opens live trading.
        """
        try:
            metadata = self.adapter.account_metadata()
        except Exception:  # noqa: BLE001 - classification reports, never raises
            return "unknown"
        return _TRADE_MODES.get((metadata or {}).get("trade_mode"), "unknown")

    def is_live(self) -> bool:
        return self.environment() not in {"demo", "contest"}

    def ensure_live_allowed(self) -> None:
        """`/v1/orders` on a real account needs LIVE_TRADING_ENABLED, as on
        cTrader. The legacy `/v1/signals` path and OCO are not gated here."""
        if not self.settings.live_trading_enabled and self.is_live():
            raise ServiceError(
                503,
                "live_trading_disabled",
                "LIVE_TRADING_ENABLED is required for orders on a live MT5 account",
                {"environment": self.environment()},
            )

    def ensure_ready(self) -> None:
        if not self.settings.trading_enabled:
            raise ServiceError(503, "trading_disabled", "Live trading is disabled by configuration")
        connection = self.adapter.connection_snapshot()
        if not self.connection_ready(connection):
            raise ServiceError(
                503,
                "terminal_not_ready",
                "The configured MT5 terminal and account are not ready for trading",
                {
                    "connected": connection.connected,
                    "account_matches": connection.login == self.settings.login,
                    "trade_allowed": connection.trade_allowed,
                    "expert_allowed": connection.expert_allowed,
                },
            )

    def safe_last_error(self) -> Any:
        try:
            return self.adapter.last_error()
        except Exception:  # noqa: BLE001 - diagnostics only
            return None

    # --- new orders: validation ----------------------------------------------

    def symbol_context(self, intent: PlaceIntent) -> SymbolContext:
        if intent.symbol not in self.settings.allowed_symbols:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                "The symbol is not in the configured MT5 symbol allowlist",
            )
        maximum = self.settings.maximum_volume
        if maximum is not None and intent.volume > maximum:
            raise ServiceError(
                422,
                "volume_cap_exceeded",
                "The requested volume exceeds MAXIMUM_VOLUME",
            )
        deviation = intent.deviation_points
        if deviation is not None and deviation > self.settings.maximum_deviation_points:
            raise ServiceError(
                422,
                "deviation_cap_exceeded",
                "deviation_points exceeds MAXIMUM_DEVIATION_POINTS",
            )

        symbol = self.visible_symbol(intent.symbol, intent.log_fields)
        tick = self.adapter.symbol_tick(intent.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            raise ServiceError(503, "tick_unavailable", "A valid current bid/ask is unavailable")

        self._validate_volume(intent.volume, symbol)
        entry, stop_loss, take_profit, adjustments = self._resolve_prices(intent, symbol, tick)
        self._validate_prices(intent, symbol, tick, entry, stop_loss, take_profit)
        return SymbolContext(symbol, tick, entry, stop_loss, take_profit, adjustments)

    def visible_symbol(self, name: str, log_fields: dict[str, Any]) -> SymbolSnapshot:
        symbol = self.adapter.symbol_info(name)
        if symbol is None:
            raise ServiceError(422, "symbol_not_found", "The broker does not expose this symbol")
        if symbol.visible:
            return symbol
        self._log("symbol_selection_started", **log_fields, symbol=name)
        if not self.adapter.symbol_select(name):
            raise ServiceError(422, "symbol_unavailable", "The symbol could not be enabled")
        symbol = self.adapter.symbol_info(name)
        if symbol is None or not symbol.visible:
            raise ServiceError(
                422,
                "symbol_unavailable",
                "The symbol is not visible after selection",
            )
        self._log(
            "symbol_selection_completed", **log_fields, symbol=name, symbol_info=asdict(symbol)
        )
        return symbol

    @staticmethod
    def _stops_level_distance(symbol: SymbolSnapshot) -> Decimal:
        return Decimal(symbol.trade_stops_level) * Decimal(str(symbol.point))

    @staticmethod
    def _spread(tick: TickSnapshot) -> Decimal:
        return Decimal(str(tick.ask)) - Decimal(str(tick.bid))

    def _minimum_stop_loss_distance(
        self,
        symbol: SymbolSnapshot,
        tick: TickSnapshot,
        *,
        market: bool,
    ) -> Decimal:
        """Minimum SL distance from the market fill price (bid/ask).

        MT5 validates protective stops from the opposite quote side, so market
        orders need trade_stops_level plus the current spread.
        """
        minimum = self._stops_level_distance(symbol)
        if not market:
            return minimum
        return minimum + self._spread(tick)

    @staticmethod
    def _minimum_take_profit_distance(symbol: SymbolSnapshot) -> Decimal:
        return Decimal(symbol.trade_stops_level) * Decimal(str(symbol.point))

    @staticmethod
    def _validate_volume(volume: Decimal, symbol: SymbolSnapshot) -> None:
        minimum = Decimal(str(symbol.volume_min))
        maximum = Decimal(str(symbol.volume_max))
        step = Decimal(str(symbol.volume_step))
        if volume < minimum or volume > maximum:
            raise ServiceError(
                422,
                "invalid_volume",
                "Volume is outside the broker's symbol limits",
                {"minimum": str(minimum), "maximum": str(maximum)},
            )
        if step <= 0:
            raise ServiceError(503, "invalid_symbol_metadata", "Broker volume_step is invalid")
        try:
            increments = (volume - minimum) / step
        except InvalidOperation as exc:  # pragma: no cover - defensive decimal guard
            raise ServiceError(422, "invalid_volume", "Volume cannot be validated") from exc
        if increments != increments.to_integral_value():
            raise ServiceError(
                422,
                "invalid_volume_step",
                "Volume does not align with the broker's volume step",
                {"minimum": str(minimum), "step": str(step)},
            )

    def _resolve_prices(
        self, intent: PlaceIntent, symbol: SymbolSnapshot, tick: TickSnapshot
    ) -> tuple[Decimal, Decimal | None, Decimal | None, dict[str, Any]]:
        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        if intent.entry_price is None:
            entry = ask if intent.direction is Direction.BUY else bid
            stop_loss = quantize_price(intent.stop_loss, symbol.digits)
            take_profit = quantize_price(intent.take_profit, symbol.digits)
            return self._apply_distances(intent, symbol, tick, entry, stop_loss, take_profit)

        entry = intent.entry_price
        self._validate_precision("entry_price", entry, symbol.digits)
        for name, price in (("stop_loss", intent.stop_loss), ("take_profit", intent.take_profit)):
            if price is not None:
                self._validate_precision(name, price, symbol.digits)
        return self._apply_distances(
            intent, symbol, tick, entry, intent.stop_loss, intent.take_profit, market=False
        )

    def _apply_distances(
        self,
        intent: PlaceIntent,
        symbol: SymbolSnapshot,
        tick: TickSnapshot,
        entry: Decimal,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        *,
        market: bool = True,
    ) -> tuple[Decimal, Decimal | None, Decimal | None, dict[str, Any]]:
        """Resolve relative SL/TP against the execution reference price.

        For market orders ``entry`` is the live ask/bid the order will fill at, so a
        distance-based level measures the intended risk from the actual fill rather
        than from a stale price the caller observed.
        """
        away = Decimal(-1) if intent.direction is Direction.BUY else Decimal(1)
        adjustments: dict[str, Any] = {}
        if intent.stop_loss_distance is not None:
            requested = Decimal(str(intent.stop_loss_distance))
            minimum = self._minimum_stop_loss_distance(symbol, tick, market=market)
            distance = max(requested, minimum)
            if distance != requested:
                adjustment = {
                    "requested_distance": str(requested),
                    "applied_distance": str(distance),
                    "minimum_distance": str(minimum),
                }
                adjustments["stop_loss"] = adjustment
                self._log("stop_loss_distance_widened", **intent.log_fields, **adjustment)
            stop_loss = quantize_price(entry + away * distance, symbol.digits)
        if intent.take_profit_distance is not None:
            requested = Decimal(str(intent.take_profit_distance))
            minimum = self._minimum_take_profit_distance(symbol)
            distance = max(requested, minimum)
            if distance != requested:
                adjustment = {
                    "requested_distance": str(requested),
                    "applied_distance": str(distance),
                    "minimum_distance": str(minimum),
                }
                adjustments["take_profit"] = adjustment
                self._log("take_profit_distance_widened", **intent.log_fields, **adjustment)
            take_profit = quantize_price(entry - away * distance, symbol.digits)
        for name, price in (("stop_loss", stop_loss), ("take_profit", take_profit)):
            if price is not None and price <= 0:
                raise ServiceError(
                    422,
                    f"{name}_distance_too_large",
                    f"{name}_distance resolves to a non-positive price",
                )
        return entry, stop_loss, take_profit, adjustments

    def _validate_prices(
        self,
        intent: PlaceIntent,
        symbol: SymbolSnapshot,
        tick: TickSnapshot,
        entry: Decimal,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
    ) -> None:
        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        if intent.execution_type is ExecutionType.LIMIT:
            valid = entry < ask if intent.direction is Direction.BUY else entry > bid
            if not valid:
                raise ServiceError(
                    422,
                    "invalid_entry_price",
                    "A buy limit must be below ask and a sell limit must be above bid",
                )
        elif intent.execution_type is ExecutionType.STOP:
            valid = entry > ask if intent.direction is Direction.BUY else entry < bid
            if not valid:
                raise ServiceError(
                    422,
                    "invalid_entry_price",
                    "A buy stop must be above ask and a sell stop must be below bid",
                )

        reference = entry
        minimum_distance = self._stops_level_distance(symbol)
        minimum_stop_distance = (
            self._minimum_stop_loss_distance(symbol, tick, market=True)
            if intent.entry_price is None
            else minimum_distance
        )
        minimum_profit_distance = self._minimum_take_profit_distance(symbol)
        if intent.entry_price is not None and minimum_distance > 0:
            if intent.execution_type is ExecutionType.LIMIT:
                entry_distance = ask - entry if intent.direction is Direction.BUY else entry - bid
            else:
                entry_distance = entry - ask if intent.direction is Direction.BUY else bid - entry
            if entry_distance < minimum_distance:
                raise ServiceError(
                    422,
                    "entry_price_too_close",
                    "entry_price violates the broker's trade_stops_level",
                )

        if intent.direction is Direction.BUY:
            if stop_loss is not None and stop_loss >= reference:
                raise ServiceError(422, "invalid_stop_loss", "Buy stop_loss must be below entry")
            if take_profit is not None and take_profit <= reference:
                raise ServiceError(
                    422,
                    "invalid_take_profit",
                    "Buy take_profit must be above entry",
                )
            stop_distance = reference - stop_loss if stop_loss is not None else None
            profit_distance = take_profit - reference if take_profit is not None else None
        else:
            if stop_loss is not None and stop_loss <= reference:
                raise ServiceError(422, "invalid_stop_loss", "Sell stop_loss must be above entry")
            if take_profit is not None and take_profit >= reference:
                raise ServiceError(
                    422,
                    "invalid_take_profit",
                    "Sell take_profit must be below entry",
                )
            stop_distance = stop_loss - reference if stop_loss is not None else None
            profit_distance = reference - take_profit if take_profit is not None else None

        if stop_distance is not None and stop_distance < minimum_stop_distance:
            raise ServiceError(422, "stop_loss_too_close", "stop_loss violates trade_stops_level")
        if profit_distance is not None and profit_distance < minimum_profit_distance:
            raise ServiceError(
                422,
                "take_profit_too_close",
                "take_profit violates trade_stops_level",
            )

    @staticmethod
    def _validate_precision(name: str, price: Decimal, digits: int) -> None:
        quantum = Decimal(1).scaleb(-digits)
        if price != price.quantize(quantum):
            raise ServiceError(
                422,
                "invalid_price_precision",
                f"{name} has more precision than the broker allows",
                {"digits": digits},
            )

    # --- new orders: the request ---------------------------------------------

    def build_request(self, intent: PlaceIntent, context: SymbolContext) -> dict[str, Any]:
        constants = self.adapter.constants
        order_type = {
            (ExecutionType.MARKET, Direction.BUY): constants.order_type_buy,
            (ExecutionType.MARKET, Direction.SELL): constants.order_type_sell,
            (ExecutionType.LIMIT, Direction.BUY): constants.order_type_buy_limit,
            (ExecutionType.LIMIT, Direction.SELL): constants.order_type_sell_limit,
            (ExecutionType.STOP, Direction.BUY): constants.order_type_buy_stop,
            (ExecutionType.STOP, Direction.SELL): constants.order_type_sell_stop,
        }[(intent.execution_type, intent.direction)]

        request: dict[str, Any] = {
            "action": (
                constants.trade_action_deal
                if intent.execution_type is ExecutionType.MARKET
                else constants.trade_action_pending
            ),
            "symbol": intent.symbol,
            "volume": float(intent.volume),
            "type": order_type,
            "price": float(context.entry),
            "sl": float(context.stop_loss or 0),
            "tp": float(context.take_profit or 0),
            "deviation": (
                intent.deviation_points
                if intent.deviation_points is not None
                else self.settings.default_deviation_points
            ),
            "magic": self.settings.magic_number,
            "comment": intent.broker_tag,
            "type_time": (
                constants.order_time_specified
                if intent.expires_at is not None
                else constants.order_time_gtc
            ),
            "type_filling": self.filling_policy(context.symbol),
        }
        if intent.expires_at is not None:
            request["expiration"] = int(intent.expires_at.timestamp())
        return request

    def filling_policy(self, symbol: SymbolSnapshot) -> int:
        constants = self.adapter.constants
        if symbol.filling_mode & constants.symbol_filling_fok:
            return constants.order_filling_fok
        if symbol.filling_mode & constants.symbol_filling_ioc:
            return constants.order_filling_ioc
        return constants.order_filling_return

    def place_outcome(self, result: dict[str, Any]) -> TargetOutcome | None:
        """The target a send result settles to, or None for a broker rejection."""
        retcode = int(result.get("retcode", -1))
        constants = self.adapter.constants
        if retcode == constants.retcode_done:
            state = TargetState.FILLED
        elif retcode == constants.retcode_done_partial:
            state = TargetState.PARTIALLY_FILLED_FINAL
        elif retcode == constants.retcode_placed:
            state = TargetState.PLACED
        else:
            return None
        return TargetOutcome(
            state,
            {
                "order_id": positive_int_or_none(result.get("order")),
                "deal_id": positive_int_or_none(result.get("deal")),
                "executed_volume_lots": decimal_or_none(result.get("volume")),
                "execution_price": decimal_or_none(result.get("price")),
            },
            {"result": result},
        )

    def match_history(
        self,
        broker_tag: str,
        request: dict[str, Any] | None,
        deals: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> tuple[TargetOutcome, dict[str, Any]] | None:
        """The deal or order a restart-interrupted send produced, by comment,
        symbol and volume; returns the outcome and the matched history row."""
        for item, state in (
            *((deal, TargetState.FILLED) for deal in deals),
            *((order, TargetState.PLACED) for order in orders),
        ):
            if not self._history_item_matches(broker_tag, request or {}, item):
                continue
            placed = state is TargetState.PLACED
            return (
                TargetOutcome(
                    state,
                    {
                        "order_id": positive_int_or_none(
                            item.get("order", item.get("ticket") if placed else None)
                        ),
                        "deal_id": positive_int_or_none(None if placed else item.get("ticket")),
                        "executed_volume_lots": decimal_or_none(item.get("volume")),
                        "execution_price": decimal_or_none(item.get("price")),
                    },
                    {"result": {"reconciled_from": item}},
                ),
                item,
            )
        return None

    @staticmethod
    def _history_item_matches(
        broker_tag: str, request: dict[str, Any], item: dict[str, Any]
    ) -> bool:
        if str(item.get("comment", "")) != broker_tag:
            return False
        symbol = request.get("symbol")
        if symbol is not None and item.get("symbol") is not None:
            if str(item["symbol"]) != str(symbol):
                return False
        volume = request.get("volume")
        if volume is not None and item.get("volume") is not None:
            if float(item["volume"]) != float(volume):
                return False
        return True

    # --- ExecutionProvider: accounts ------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return (self.account,)

    def account_statuses(self) -> list[dict[str, Any]]:
        ready, details = self.readiness()
        environment = self.environment()
        is_live = environment not in {"demo", "contest"}
        available = ready and self.settings.trading_enabled
        orders_allowed = available and (self.settings.live_trading_enabled or not is_live)
        return [
            {
                "alias": self.account,
                "provider": self.name,
                "ctid_trader_account_id": None,
                "environment": environment,
                "is_live": is_live,
                "connected": bool(details.get("terminal_connected")),
                "reconciled": self.initialized,
                "broker_access_rights": None,
                "available_for_trading": available,
                "order_entry_enabled": orders_allowed,
                "position_close_enabled": orders_allowed,
            }
        ]

    def _require_account(self, account: str) -> None:
        if account != self.account:
            raise KeyError(f"Unknown or disabled account alias: {account}")

    def orders(self, account: str) -> list[BrokerOrder]:
        self._require_account(account)
        return [
            BrokerOrder(
                account=self.account,
                order_id=int(row["ticket"]),
                position_id=positive_int_or_none(row.get("position_id")),
                client_order_id=str(row.get("comment") or "") or None,
                instrument=row.get("symbol"),
                volume_lots=decimal_or_none(row.get("volume_current", row.get("volume"))),
                state="placed",
            )
            for row in self.adapter.active_orders()
        ]

    def positions(self, account: str) -> list[BrokerPosition]:
        self._require_account(account)
        return [
            BrokerPosition(
                account=self.account,
                position_id=int(row["ticket"]),
                instrument=row.get("symbol"),
                volume_lots=decimal_or_none(row.get("volume")),
                direction=Direction.BUY if int(row.get("type", 0)) == 0 else Direction.SELL,
                price=decimal_or_none(row.get("price_open")),
                stop_loss=decimal_or_none(row.get("sl")) or None,
                take_profit=decimal_or_none(row.get("tp")) or None,
            )
            for row in self.adapter.active_positions()
        ]

    # --- ExecutionProvider: operations ----------------------------------------

    def client_order_id(self, operation_id: UUID, account: str) -> str:
        """MT5 has no client order ID; the order comment carries it instead.

        Comments are capped at 31 characters by the terminal. 24 hex characters
        of the operation ID keep collisions out of reach for one account.
        """
        del account
        return f"o-{operation_id.hex[:24]}"

    def _symbol_for(self, instrument: str) -> str:
        """``OrderRequest`` upper-cases its instrument; MT5 names are
        case-sensitive ("Volatility 75 Index"), so match case-insensitively."""
        for symbol in self.settings.allowed_symbols:
            if symbol.upper() == instrument.upper():
                return symbol
        raise ServiceError(
            422,
            "symbol_not_allowed",
            "The symbol is not in the configured MT5 symbol allowlist",
        )

    async def prepare(
        self,
        action: OperationAction,
        request: Any,
        target: Any,
        client_order_id: str,
    ) -> PreparedRequest:
        self._require_account(target.account)
        async with self._lock:
            return await asyncio.to_thread(
                self._prepare_sync, action, request, target, client_order_id
            )

    def _prepare_sync(
        self,
        action: OperationAction,
        request: Any,
        target: Any,
        client_order_id: str,
    ) -> PreparedRequest:
        self.ensure_ready()
        self.ensure_live_allowed()
        constants = self.adapter.constants
        if action is OperationAction.PLACE_ORDER:
            assert isinstance(request, OrderRequest) and isinstance(target, OrderTarget)
            intent = PlaceIntent(
                symbol=self._symbol_for(request.instrument),
                direction=request.direction,
                execution_type=request.execution_type,
                volume=target.volume_lots,
                broker_tag=client_order_id,
                entry_price=request.entry_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                stop_loss_distance=request.stop_loss_distance,
                take_profit_distance=request.take_profit_distance,
                expires_at=request.expires_at,
                log_fields={"operation_id": str(request.operation_id)},
            )
            context = self.symbol_context(intent)
            return PreparedRequest(self.build_request(intent, context), TargetState.FILLED)

        if action is OperationAction.CANCEL_ORDER:
            assert isinstance(target, OrderReferenceTarget)
            self._active_order(target.order_id)
            return PreparedRequest(
                {
                    "action": constants.trade_action_remove,
                    "order": target.order_id,
                    "comment": client_order_id,
                },
                TargetState.CANCELLED,
                preflight=False,
            )

        if action is OperationAction.AMEND_ORDER:
            assert isinstance(target, AmendOrderTarget)
            order = self._active_order(target.order_id)
            if target.volume_lots is not None:
                raise ServiceError(
                    422,
                    "amend_volume_not_supported",
                    "MT5 cannot change the volume of a pending order; cancel and replace it",
                )
            if (
                target.entry_price is None
                and target.stop_loss is None
                and target.take_profit is None
                and target.expires_at is None
            ):
                raise ServiceError(422, "empty_amendment", "At least one order field is required")
            symbol = self.visible_symbol(str(order["symbol"]), {})
            amended: dict[str, Any] = {
                "action": constants.trade_action_modify,
                "order": target.order_id,
                "symbol": order["symbol"],
                "price": float(target.entry_price or Decimal(str(order.get("price_open", 0)))),
                "sl": float(target.stop_loss or Decimal(str(order.get("sl", 0)))),
                "tp": float(target.take_profit or Decimal(str(order.get("tp", 0)))),
                "type_time": int(order.get("type_time", constants.order_time_gtc)),
                "comment": client_order_id,
            }
            for name, price in (
                ("entry_price", target.entry_price),
                ("stop_loss", target.stop_loss),
                ("take_profit", target.take_profit),
            ):
                if price is not None:
                    self._validate_precision(name, price, symbol.digits)
            if target.expires_at is not None:
                amended["type_time"] = constants.order_time_specified
                amended["expiration"] = int(target.expires_at.timestamp())
            elif order.get("time_expiration"):
                amended["expiration"] = int(order["time_expiration"])
            return PreparedRequest(amended, TargetState.AMENDED, preflight=False)

        if action is OperationAction.AMEND_POSITION:
            assert isinstance(target, PositionProtectionTarget)
            if target.stop_loss is None and target.take_profit is None:
                raise ServiceError(422, "empty_amendment", "stop_loss or take_profit is required")
            if target.trailing_stop_loss:
                raise ServiceError(
                    422,
                    "trailing_stop_not_supported",
                    "MT5 trailing stops run in the terminal, not through the trade API",
                )
            position = self._active_position(target.position_id)
            symbol = self.visible_symbol(str(position["symbol"]), {})
            for name, price in (
                ("stop_loss", target.stop_loss),
                ("take_profit", target.take_profit),
            ):
                if price is not None:
                    self._validate_precision(name, price, symbol.digits)
            # TRADE_ACTION_SLTP sets both levels; 0 removes one. Keep the side
            # the caller did not mention.
            return PreparedRequest(
                {
                    "action": constants.trade_action_sltp,
                    "position": target.position_id,
                    "symbol": position["symbol"],
                    "sl": float(target.stop_loss or Decimal(str(position.get("sl", 0)))),
                    "tp": float(target.take_profit or Decimal(str(position.get("tp", 0)))),
                    "magic": self.settings.magic_number,
                    "comment": client_order_id,
                },
                TargetState.AMENDED,
            )

        if action is OperationAction.CLOSE_POSITION:
            assert isinstance(target, ClosePositionTarget)
            position = self._active_position(target.position_id)
            open_volume = Decimal(str(position.get("volume", 0)))
            if target.volume_lots > open_volume:
                raise ServiceError(
                    422, "close_volume_too_large", "Close volume exceeds the open position"
                )
            symbol = self.visible_symbol(str(position["symbol"]), {})
            self._validate_volume(target.volume_lots, symbol)
            tick = self.adapter.symbol_tick(str(position["symbol"]))
            if tick is None or tick.bid <= 0 or tick.ask <= 0:
                raise ServiceError(
                    503, "tick_unavailable", "A valid current bid/ask is unavailable"
                )
            is_buy = int(position.get("type", 0)) == 0
            return PreparedRequest(
                {
                    "action": constants.trade_action_deal,
                    "position": target.position_id,
                    "symbol": position["symbol"],
                    "volume": float(target.volume_lots),
                    "type": constants.order_type_sell if is_buy else constants.order_type_buy,
                    "price": tick.bid if is_buy else tick.ask,
                    "deviation": self.settings.default_deviation_points,
                    "magic": self.settings.magic_number,
                    "comment": client_order_id,
                    "type_time": constants.order_time_gtc,
                    "type_filling": self.filling_policy(symbol),
                },
                TargetState.CLOSED,
            )

        raise ServiceError(501, "action_not_supported", f"MT5 does not support {action.value}")

    def _active_order(self, order_id: int) -> dict[str, Any]:
        for order in self.adapter.active_orders():
            if int(order.get("ticket", 0)) == order_id:
                return order
        raise ServiceError(
            422,
            "order_not_found",
            "Pending order is not present in the terminal",
            {"account": self.account, "order_id": order_id},
        )

    def _active_position(self, position_id: int) -> dict[str, Any]:
        for position in self.adapter.active_positions():
            if int(position.get("ticket", 0)) == position_id:
                return position
        raise ServiceError(422, "position_not_found", "Position is not open in the terminal")

    async def dispatch(
        self,
        operation_id: UUID,
        account: str,
        action: OperationAction,
        prepared: PreparedRequest,
        client_order_id: str,
    ) -> TargetOutcome:
        """Shielded: if the caller times out, the send already in the thread
        still completes under the lock, so no second call overlaps it."""
        del operation_id, account, client_order_id
        return await asyncio.shield(self._locked_dispatch(action, prepared))

    async def _locked_dispatch(
        self, action: OperationAction, prepared: PreparedRequest
    ) -> TargetOutcome:
        async with self._lock:
            return await asyncio.to_thread(self._dispatch_sync, action, prepared)

    def _dispatch_sync(self, action: OperationAction, prepared: PreparedRequest) -> TargetOutcome:
        request = prepared.request
        details: dict[str, Any] = {"request": request}
        if prepared.preflight:
            try:
                check = self.adapter.order_check(request)
            except Exception as exc:  # noqa: BLE001 - nothing was sent yet
                return self._rejected("mt5_validation_unavailable", type(exc).__name__, details)
            details["check"] = check
            if check is None:
                return self._rejected(
                    "mt5_preflight_unavailable", str(self.safe_last_error()), details
                )
            if int(check.get("retcode", -1)) != 0:
                return self._rejected("preflight_rejected", str(check.get("comment")), details)
        try:
            result = self.adapter.order_send(request)
        except Exception as exc:  # noqa: BLE001 - outcome unknown, never retried
            return TargetOutcome(
                TargetState.UNKNOWN,
                {"error_code": "execution_outcome_unknown", "error_message": type(exc).__name__},
                details,
            )
        if result is None:
            return TargetOutcome(
                TargetState.UNKNOWN,
                {
                    "error_code": "execution_outcome_unknown",
                    "error_message": str(self.safe_last_error()),
                },
                details,
            )
        details["result"] = result
        if action is OperationAction.PLACE_ORDER:
            outcome = self.place_outcome(result)
            if outcome is not None:
                return TargetOutcome(outcome.state, outcome.values, details)
        elif int(result.get("retcode", -1)) == self.adapter.constants.retcode_done:
            values: dict[str, Any] = {"error_code": None}
            if action is OperationAction.CLOSE_POSITION:
                values |= {
                    "deal_id": positive_int_or_none(result.get("deal")),
                    "executed_volume_lots": decimal_or_none(result.get("volume")),
                    "execution_price": decimal_or_none(result.get("price")),
                }
            return TargetOutcome(prepared.success_state, values, details)
        return TargetOutcome(
            TargetState.REJECTED,
            {
                "error_code": str(result.get("retcode")),
                "error_message": str(result.get("comment") or "") or None,
            },
            details,
        )

    @staticmethod
    def _rejected(code: str, message: str, details: dict[str, Any]) -> TargetOutcome:
        return TargetOutcome(
            TargetState.REJECTED, {"error_code": code, "error_message": message}, details
        )

    async def reconcile(self) -> None:
        """Settle `/v1/orders` targets a restart interrupted, and any UNKNOWN
        ones the terminal's history can now account for.

        Only targets with a client order ID are this method's: legacy
        `/v1/signals` rows carry the signal source as their comment and are
        reconciled by the signal path, which owns their stored response body.
        """
        await self._reconcile({TargetState.RESERVED, TargetState.DISPATCHED, TargetState.UNKNOWN})

    async def reconcile_unknown(self) -> None:
        await self._reconcile({TargetState.UNKNOWN})

    async def _reconcile(self, states: set[TargetState]) -> None:
        ledger = self._ledger
        if ledger is None:
            return
        horizon = datetime.now(UTC) - UNKNOWN_RECONCILE_WINDOW
        pending = [
            target
            for target in ledger.unresolved_targets([self.account])
            if target.client_order_id is not None
            and target.state in states
            and (target.state is not TargetState.UNKNOWN or target.created_at >= horizon)
        ]
        if not pending:
            return
        async with self._lock:
            await asyncio.to_thread(self._reconcile_sync, ledger, pending)

    def _reconcile_sync(self, ledger: LedgerPort, pending: list[Any]) -> None:
        for target in pending:
            if target.state is TargetState.RESERVED:
                ledger.update_target(
                    target.operation_id,
                    target.account,
                    TargetState.REJECTED,
                    error_code="restart_before_execution",
                    error_message="The service restarted before this target reached the broker",
                )
        dispatched = [target for target in pending if target.state is TargetState.DISPATCHED]
        unknown = [target for target in pending if target.state is TargetState.UNKNOWN]
        if not dispatched and not unknown:
            return
        try:
            if not self.connection_ready(self.adapter.connection_snapshot()):
                raise RuntimeError("terminal is not ready for reconciliation")
            start = min(target.created_at for target in (*dispatched, *unknown))
            start -= timedelta(minutes=5)
            end = datetime.now(UTC) + timedelta(minutes=1)
            deals = self.adapter.history_deals(start, end)
            orders = self.adapter.history_orders(start, end)
        except Exception as exc:  # noqa: BLE001 - leave the outcome explicitly unknown
            # UNKNOWN targets already say so; only in-flight ones move.
            for target in dispatched:
                ledger.update_target(
                    target.operation_id,
                    target.account,
                    TargetState.UNKNOWN,
                    error_code="execution_outcome_unknown",
                    error_message=type(exc).__name__,
                )
            return
        settled: list[str] = []
        for target in (*dispatched, *unknown):
            outcome = self._history_outcome(ledger, target, deals, orders)
            if outcome is None:
                if target.state is TargetState.DISPATCHED:
                    ledger.update_target(
                        target.operation_id,
                        target.account,
                        TargetState.UNKNOWN,
                        error_code="execution_outcome_unknown",
                        error_message="No matching MT5 order or deal was found after restart",
                    )
                # An UNKNOWN target stays UNKNOWN: no match is not proof the
                # send failed, so it is never flipped to rejected.
                continue
            ledger.update_target(
                target.operation_id,
                target.account,
                outcome.state,
                details=outcome.details,
                **({"error_code": None, "error_message": None} | outcome.values),
            )
            settled.append(target.operation_id)
        if settled or dispatched:
            self._log(
                "mt5_operations_reconciled",
                operation_ids=[target.operation_id for target in dispatched],
                settled=settled,
            )

    def _history_outcome(
        self,
        ledger: LedgerPort,
        target: Any,
        deals: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> TargetOutcome | None:
        """What the history says a target did, for actions history can prove.

        A placed order leaves a deal (filled) or an order (resting); a close
        leaves a deal. Cancels and amendments leave nothing tagged with the
        client order ID, so they stay with whatever state they had.
        """
        operation = ledger.get(target.operation_id)
        if operation is None:
            return None
        current = next((t for t in operation.targets if t.account == target.account), None)
        if current is None or current.state is not target.state:
            return None  # settled since it was listed (a late dispatch outcome)
        action = operation.action
        if action not in {OperationAction.PLACE_ORDER, OperationAction.CLOSE_POSITION}:
            return None
        request = (target.details or {}).get("request")
        if action is OperationAction.CLOSE_POSITION:
            orders = []
        matched = self.match_history(target.client_order_id, request, deals, orders)
        if matched is None:
            return None
        outcome, _ = matched
        if action is OperationAction.CLOSE_POSITION:
            return TargetOutcome(TargetState.CLOSED, outcome.values, outcome.details)
        return outcome
