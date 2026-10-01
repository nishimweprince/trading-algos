"""The entry point execution-service and market-data-service discover."""

from __future__ import annotations

from typing import Any

from ta_plugin_api.hub import MarketDataHub

from .gateway import CTraderGateway
from .session import CTraderSession

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
        return CTraderGateway(settings)

    def session(self, settings: Any, hub: MarketDataHub) -> CTraderSession:
        """One account, market data only (the legacy CTRADER_ACCOUNT_ID mode)."""
        return CTraderSession(settings, hub)


FACTORY = CTraderFactory()
