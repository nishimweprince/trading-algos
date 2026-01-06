"""Mock data feed for testing and backtesting"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict

from loguru import logger


class MockDataFeed:
    """Mock data feed that generates synthetic price data"""
    
    def __init__(self):
        logger.info("Using MockDataFeed")

    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500, **kwargs) -> pd.DataFrame:
        dates = pd.date_range(end=datetime.now(), periods=count, freq='1h')
        np.random.seed(42)
        returns = np.random.randn(count) * 0.001
        close = 1.1000 * np.exp(np.cumsum(returns))
        high = close * (1 + np.abs(np.random.randn(count)) * 0.001)
        low = close * (1 - np.abs(np.random.randn(count)) * 0.001)
        return pd.DataFrame({
            'open': low + (high - low) * np.random.rand(count),
            'high': high,
            'low': low,
            'close': close,
            'volume': np.random.randint(1000, 10000, count)
        }, index=dates)

    def get_current_price(self, instrument: str) -> Dict[str, float]:
        return {'bid': 1.0995, 'ask': 1.1005, 'mid': 1.1000, 'spread': 0.0010}

    def get_multi_timeframe_data(self, instrument: str, tf_current: str = '1H', tf_higher: str = '4H', count: int = 500):
        return {
            'current': self.get_candles(instrument, tf_current, count),
            'htf': self.get_candles(instrument, tf_higher, count // 4)
        }

