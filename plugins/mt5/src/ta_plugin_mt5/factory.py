"""The entry point execution-service and market-data-service discover."""

from __future__ import annotations

from typing import Any

from .market_data import MT5MarketData
from .terminal import MT5Adapter

__all__ = ["FACTORY", "MT5Factory"]


class MT5Factory:
    name = "mt5"

    def missing_settings(self, settings: Any) -> list[str]:
        return [
            alias
            for alias, value in (
                ("MT5_TERMINAL_PATH", settings.terminal_path),
                ("MT5_LOGIN", settings.login),
                ("MT5_PASSWORD", settings.password),
                ("MT5_SERVER", settings.server),
            )
            if value is None
        ]

    def terminal(self) -> MT5Adapter:
        """The real terminal. Imports MetaTrader5 only now, so this module stays
        importable on macOS and Linux, where that package does not exist."""
        from .terminal import RealMT5Adapter

        return RealMT5Adapter()

    def market_data(self, settings: Any) -> MT5MarketData:
        """Quotes and closed UTC candles from this host's one terminal."""
        return MT5MarketData(self.terminal(), settings)


FACTORY = MT5Factory()
