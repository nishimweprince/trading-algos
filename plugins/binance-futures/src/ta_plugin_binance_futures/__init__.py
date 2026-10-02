"""Binance USDⓈ-M futures provider: market data, verified local books, raw streams.

Services reach it through the ``binance_futures`` entry point in the
``ta.market_data`` group (see ta-plugin-api), not by importing it to pick a
provider. Types (events, ``LocalOrderBook``, settings mixin) may be imported.
"""

from .factory import FACTORY, BinanceFuturesFactory

__all__ = ["FACTORY", "BinanceFuturesFactory"]
