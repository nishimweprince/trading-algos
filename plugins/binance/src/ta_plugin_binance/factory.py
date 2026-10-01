"""The entry point market-data-service discovers."""

from __future__ import annotations

from typing import Any

from .market_data import BinanceMarketData

__all__ = ["FACTORY", "BinanceFactory"]


class BinanceFactory:
    name = "binance"

    def missing_settings(self, settings: Any) -> list[str]:
        return [] if settings.binance_symbols else ["BINANCE_SYMBOLS"]

    def market_data(self, settings: Any) -> BinanceMarketData:
        """Public Spot data for BINANCE_SYMBOLS; no key, no account."""
        return BinanceMarketData(settings)


FACTORY = BinanceFactory()
