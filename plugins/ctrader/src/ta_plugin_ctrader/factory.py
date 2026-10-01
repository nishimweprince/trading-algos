"""The entry point execution-service and market-data-service discover."""

from __future__ import annotations

from typing import Any

from .gateway import CTraderGateway
from .market_data import CTraderMarketData

__all__ = ["FACTORY", "CTraderFactory"]


class CTraderFactory:
    name = "ctrader"

    def missing_settings(self, settings: Any) -> list[str]:
        return [
            alias
            for alias, value in (
                ("CTRADER_CLIENT_ID", settings.client_id),
                ("CTRADER_CLIENT_SECRET", settings.client_secret),
                ("CTRADER_ACCESS_TOKEN", settings.access_token),
            )
            if value is None
        ]

    def gateway(self, settings: Any) -> CTraderGateway:
        """Every account in the registry, with execution. Needs ACCOUNTS_CONFIG_PATH."""
        if not settings.gateway_enabled:
            raise ValueError("cTrader needs ACCOUNTS_CONFIG_PATH naming at least one account")
        return CTraderGateway(settings)

    def market_data(self, settings: Any) -> CTraderMarketData:
        """Quotes and candles for every registry account; each alias is a feed.

        Give this process its own OAuth grant and TOKEN_CACHE_PATH: the refresh
        token rotates, so sharing either with execution-service locks one out.
        """
        return CTraderMarketData(self.gateway(settings))


FACTORY = CTraderFactory()
