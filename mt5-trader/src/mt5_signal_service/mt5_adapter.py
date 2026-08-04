from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .config import Settings


@dataclass(frozen=True)
class MT5Constants:
    trade_action_deal: int
    trade_action_pending: int
    order_type_buy: int
    order_type_sell: int
    order_type_buy_limit: int
    order_type_sell_limit: int
    order_type_buy_stop: int
    order_type_sell_stop: int
    order_time_gtc: int
    order_time_specified: int
    order_filling_fok: int
    order_filling_ioc: int
    order_filling_return: int
    symbol_filling_fok: int
    symbol_filling_ioc: int
    retcode_placed: int
    retcode_done: int
    retcode_done_partial: int
    timeframes: dict[str, int]


@dataclass(frozen=True)
class SymbolSnapshot:
    name: str
    visible: bool
    digits: int
    point: float
    trade_stops_level: int
    volume_min: float
    volume_max: float
    volume_step: float
    filling_mode: int


@dataclass(frozen=True)
class TickSnapshot:
    bid: float
    ask: float


@dataclass(frozen=True)
class ConnectionSnapshot:
    connected: bool
    login: int | None
    trade_allowed: bool
    expert_allowed: bool
    reason: str | None = None


class MT5Adapter(Protocol):
    constants: MT5Constants

    def initialize(self, settings: Settings) -> bool: ...

    def shutdown(self) -> None: ...

    def connection_snapshot(self) -> ConnectionSnapshot: ...

    def symbol_info(self, symbol: str) -> SymbolSnapshot | None: ...

    def symbol_select(self, symbol: str) -> bool: ...

    def symbol_tick(self, symbol: str) -> TickSnapshot | None: ...

    def order_check(self, request: dict[str, Any]) -> dict[str, Any] | None: ...

    def order_send(self, request: dict[str, Any]) -> dict[str, Any] | None: ...

    def history_orders(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def history_deals(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def copy_rates(
        self, symbol: str, timeframe: int, count: int
    ) -> list[dict[str, Any]] | None: ...

    def last_error(self) -> Any: ...


def _plain(value: Any) -> Any:
    if hasattr(value, "_asdict"):
        return {key: _plain(item) for key, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class RealMT5Adapter:
    """Thin lazy wrapper around the Windows-only MetaTrader5 package."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:  # pragma: no cover - exercised only outside Windows
            message = "MetaTrader5 is available only on the Windows execution host"
            raise RuntimeError(message) from exc

        self._mt5 = mt5
        self.constants = MT5Constants(
            trade_action_deal=mt5.TRADE_ACTION_DEAL,
            trade_action_pending=mt5.TRADE_ACTION_PENDING,
            order_type_buy=mt5.ORDER_TYPE_BUY,
            order_type_sell=mt5.ORDER_TYPE_SELL,
            order_type_buy_limit=mt5.ORDER_TYPE_BUY_LIMIT,
            order_type_sell_limit=mt5.ORDER_TYPE_SELL_LIMIT,
            order_type_buy_stop=mt5.ORDER_TYPE_BUY_STOP,
            order_type_sell_stop=mt5.ORDER_TYPE_SELL_STOP,
            order_time_gtc=mt5.ORDER_TIME_GTC,
            order_time_specified=mt5.ORDER_TIME_SPECIFIED,
            order_filling_fok=mt5.ORDER_FILLING_FOK,
            order_filling_ioc=mt5.ORDER_FILLING_IOC,
            order_filling_return=mt5.ORDER_FILLING_RETURN,
            symbol_filling_fok=getattr(mt5, "SYMBOL_FILLING_FOK", 1),
            symbol_filling_ioc=getattr(mt5, "SYMBOL_FILLING_IOC", 2),
            retcode_placed=mt5.TRADE_RETCODE_PLACED,
            retcode_done=mt5.TRADE_RETCODE_DONE,
            retcode_done_partial=mt5.TRADE_RETCODE_DONE_PARTIAL,
            timeframes={
                "M1": mt5.TIMEFRAME_M1,
                "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1,
                "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            },
        )

    def initialize(self, settings: Settings) -> bool:
        return bool(
            self._mt5.initialize(
                str(settings.terminal_path),
                login=settings.login,
                password=settings.password.get_secret_value(),
                server=settings.server,
                timeout=settings.mt5_timeout_ms,
            )
        )

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def connection_snapshot(self) -> ConnectionSnapshot:
        terminal = self._mt5.terminal_info()
        account = self._mt5.account_info()
        if terminal is None or account is None:
            return ConnectionSnapshot(False, None, False, False, str(self.last_error()))
        return ConnectionSnapshot(
            connected=bool(terminal.connected),
            login=int(account.login),
            trade_allowed=bool(account.trade_allowed),
            expert_allowed=bool(terminal.trade_allowed),
        )

    def symbol_info(self, symbol: str) -> SymbolSnapshot | None:
        info = self._mt5.symbol_info(symbol)
        if info is None:
            return None
        return SymbolSnapshot(
            name=info.name,
            visible=bool(info.visible),
            digits=int(info.digits),
            point=float(info.point),
            trade_stops_level=int(info.trade_stops_level),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            filling_mode=int(info.filling_mode),
        )

    def symbol_select(self, symbol: str) -> bool:
        return bool(self._mt5.symbol_select(symbol, True))

    def symbol_tick(self, symbol: str) -> TickSnapshot | None:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return TickSnapshot(float(tick.bid), float(tick.ask))

    def order_check(self, request: dict[str, Any]) -> dict[str, Any] | None:
        result = self._mt5.order_check(request)
        return None if result is None else _plain(result)

    def order_send(self, request: dict[str, Any]) -> dict[str, Any] | None:
        result = self._mt5.order_send(request)
        return None if result is None else _plain(result)

    def history_orders(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return _plain(self._mt5.history_orders_get(start, end) or [])

    def history_deals(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return _plain(self._mt5.history_deals_get(start, end) or [])

    def copy_rates(self, symbol: str, timeframe: int, count: int) -> list[dict[str, Any]] | None:
        rates = self._mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None:
            return None
        return [
            {
                "time": int(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["tick_volume"]),
            }
            for row in rates
        ]

    def last_error(self) -> Any:
        return _plain(self._mt5.last_error())
