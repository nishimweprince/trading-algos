from __future__ import annotations

import asyncio
from typing import Any

from .config import Settings
from .errors import ServiceError
from .models import Candle, CandlesResponse, Timeframe
from .mt5_adapter import MT5Adapter


class MarketDataService:
    def __init__(self, settings: Settings, adapter: MT5Adapter) -> None:
        self.settings = settings
        self.adapter = adapter

    async def get_candles(
        self, symbol: str, timeframe: Timeframe, count: int
    ) -> CandlesResponse:
        return await asyncio.to_thread(self._get_candles_sync, symbol, timeframe, count)

    def _get_candles_sync(
        self, symbol: str, timeframe: Timeframe, count: int
    ) -> CandlesResponse:
        self._ensure_connected()
        self._validate_count(count)
        self._validate_symbol(symbol)

        mt5_timeframe = self.adapter.constants.timeframes[timeframe.value]
        rates = self.adapter.copy_rates(symbol, mt5_timeframe, count)
        if rates is None:
            raise ServiceError(
                503,
                "candles_unavailable",
                "MT5 did not return candle data",
                {"last_error": self._safe_last_error()},
            )
        return CandlesResponse(
            symbol=symbol,
            timeframe=timeframe,
            candles=[Candle(**row) for row in rates],
        )

    def _ensure_connected(self) -> None:
        connection = self.adapter.connection_snapshot()
        if not connection.connected:
            raise ServiceError(
                503,
                "terminal_not_ready",
                "The MT5 terminal is not connected",
                {"connected": connection.connected},
            )

    def _validate_count(self, count: int) -> None:
        if count > self.settings.max_candles_lookback:
            raise ServiceError(
                422,
                "count_exceeds_limit",
                "count exceeds MAX_CANDLES_LOOKBACK",
                {"maximum": self.settings.max_candles_lookback},
            )

    def _validate_symbol(self, symbol: str) -> None:
        if symbol not in self.settings.allowed_symbols:
            raise ServiceError(422, "symbol_not_allowed", "The symbol is not in ALLOWED_SYMBOLS")

        info = self.adapter.symbol_info(symbol)
        if info is None:
            raise ServiceError(422, "symbol_not_found", "The broker does not expose this symbol")
        if not info.visible and not self.adapter.symbol_select(symbol):
            raise ServiceError(422, "symbol_unavailable", "The symbol could not be enabled")

    def _safe_last_error(self) -> Any:
        try:
            return self.adapter.last_error()
        except Exception:
            return None
