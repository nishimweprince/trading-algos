"""Binance Spot provider: public REST and WebSocket market data.

Services reach it through the ``binance`` entry point in the ``ta.market_data``
group (see ta-plugin-api), not by importing it to pick a provider.
"""

from .factory import FACTORY, BinanceFactory

__all__ = ["FACTORY", "BinanceFactory"]
