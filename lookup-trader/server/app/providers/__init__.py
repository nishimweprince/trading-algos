"""Market-data provider contracts and adapters."""

from app.providers.base import Candle
from app.providers.capital import CapitalMarketDataClient

__all__ = ["Candle", "CapitalMarketDataClient"]
