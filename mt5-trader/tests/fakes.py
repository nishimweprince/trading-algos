from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from mt5_signal_service.config import Settings
from mt5_signal_service.mt5_adapter import (
    ConnectionSnapshot,
    MT5Constants,
    SymbolSnapshot,
    TickSnapshot,
)

CONSTANTS = MT5Constants(
    trade_action_deal=1,
    trade_action_pending=5,
    order_type_buy=0,
    order_type_sell=1,
    order_type_buy_limit=2,
    order_type_sell_limit=3,
    order_type_buy_stop=4,
    order_type_sell_stop=5,
    order_time_gtc=0,
    order_time_specified=2,
    order_filling_fok=0,
    order_filling_ioc=1,
    order_filling_return=2,
    symbol_filling_fok=1,
    symbol_filling_ioc=2,
    retcode_placed=10008,
    retcode_done=10009,
    retcode_done_partial=10010,
    timeframes={"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440},
)


class FakeMT5Adapter:
    constants = CONSTANTS

    def __init__(self) -> None:
        self.initialized = False
        self.shutdown_called = False
        self.connection = ConnectionSnapshot(True, 123456, True, True)
        self.symbol = SymbolSnapshot(
            name="EURUSD",
            visible=True,
            digits=5,
            point=0.00001,
            trade_stops_level=10,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=1,
        )
        self.tick = TickSnapshot(bid=1.10000, ask=1.10020)
        self.check_result: dict[str, Any] | None = {"retcode": 0, "comment": "Done"}
        self.send_result: dict[str, Any] | None = {
            "retcode": 10009,
            "order": 1001,
            "deal": 2001,
            "volume": 0.1,
            "price": 1.10020,
            "comment": "Request executed",
        }
        self.check_requests: list[dict[str, Any]] = []
        self.send_requests: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []
        self.rates: list[dict[str, Any]] | None = [
            {
                "time": 1_700_000_000 + i * 60,
                "open": 1.1000 + i * 0.0001,
                "high": 1.1005 + i * 0.0001,
                "low": 1.0995 + i * 0.0001,
                "close": 1.1002 + i * 0.0001,
                "volume": 100 + i,
            }
            for i in range(5)
        ]
        self.copy_rates_calls: list[tuple[str, int, int]] = []
        self.send_delay = 0.0
        self.max_active_sends = 0
        self._active_sends = 0
        self._send_lock = threading.Lock()
        self.send_exception: Exception | None = None

    def initialize(self, _settings: Settings) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def connection_snapshot(self) -> ConnectionSnapshot:
        return self.connection

    def symbol_info(self, symbol: str) -> SymbolSnapshot | None:
        if symbol == self.symbol.name:
            return self.symbol
        return SymbolSnapshot(
            name=symbol,
            visible=True,
            digits=self.symbol.digits,
            point=self.symbol.point,
            trade_stops_level=self.symbol.trade_stops_level,
            volume_min=self.symbol.volume_min,
            volume_max=self.symbol.volume_max,
            volume_step=self.symbol.volume_step,
            filling_mode=self.symbol.filling_mode,
        )

    def symbol_select(self, symbol: str) -> bool:
        if symbol == self.symbol.name:
            self.symbol = SymbolSnapshot(**{**self.symbol.__dict__, "visible": True})
        return True

    def symbol_tick(self, symbol: str) -> TickSnapshot | None:
        if symbol == self.symbol.name:
            return self.tick
        return self.tick

    def order_check(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.check_requests.append(request.copy())
        return self.check_result

    def order_send(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.send_requests.append(request.copy())
        with self._send_lock:
            self._active_sends += 1
            self.max_active_sends = max(self.max_active_sends, self._active_sends)
        try:
            if self.send_delay:
                time.sleep(self.send_delay)
            if self.send_exception is not None:
                raise self.send_exception
            return self.send_result
        finally:
            with self._send_lock:
                self._active_sends -= 1

    def history_orders(self, _start: datetime, _end: datetime) -> list[dict[str, Any]]:
        return self.orders

    def history_deals(self, _start: datetime, _end: datetime) -> list[dict[str, Any]]:
        return self.deals

    def copy_rates(self, symbol: str, timeframe: int, count: int) -> list[dict[str, Any]] | None:
        self.copy_rates_calls.append((symbol, timeframe, count))
        return self.rates

    def last_error(self) -> Any:
        return (-1, "fake error")
