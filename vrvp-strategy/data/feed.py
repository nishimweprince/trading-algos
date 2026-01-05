"""OANDA Data Feed Module"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, Dict
from loguru import logger

try:
    from oandapyV20 import API
    from oandapyV20.endpoints import instruments, pricing
    from oandapyV20.exceptions import V20Error
    OANDA_AVAILABLE = True
except ImportError:
    OANDA_AVAILABLE = False

class OANDADataFeed:
    GRANULARITY_MAP = {'1M': 'M1', '5M': 'M5', '15M': 'M15', '30M': 'M30', '1H': 'H1', '4H': 'H4', '1D': 'D'}

    def __init__(self, api_token: str, account_id: str, environment: str = 'practice'):
        if not OANDA_AVAILABLE:
            raise ImportError("oandapyV20 required")
        self.account_id = account_id
        self.api = API(access_token=api_token, environment=environment)

    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500,
                    from_time: Optional[datetime] = None, to_time: Optional[datetime] = None) -> pd.DataFrame:
        params = {'granularity': self.GRANULARITY_MAP.get(granularity, granularity), 'count': min(count, 5000)}
        if from_time: params['from'] = from_time.isoformat() + 'Z'
        if to_time: params['to'] = to_time.isoformat() + 'Z'

        request = instruments.InstrumentsCandles(instrument=instrument, params=params)
        response = self.api.request(request)

        data = []
        for candle in response.get('candles', []):
            if candle['complete']:
                mid = candle['mid']
                data.append({'timestamp': pd.Timestamp(candle['time']), 'open': float(mid['o']),
                            'high': float(mid['h']), 'low': float(mid['l']), 'close': float(mid['c']),
                            'volume': int(candle['volume'])})

        df = pd.DataFrame(data)
        if not df.empty:
            df.set_index('timestamp', inplace=True)
            df.index = df.index.tz_localize(None)
        return df

    def get_current_price(self, instrument: str) -> Dict[str, float]:
        request = pricing.PricingInfo(accountID=self.account_id, params={'instruments': instrument})
        response = self.api.request(request)
        price = response['prices'][0]
        bid, ask = float(price['bids'][0]['price']), float(price['asks'][0]['price'])
        return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2, 'spread': ask - bid}

    def get_multi_timeframe_data(self, instrument: str, tf_current: str = '1H', tf_higher: str = '4H', count: int = 500) -> Dict[str, pd.DataFrame]:
        return {'current': self.get_candles(instrument, tf_current, count),
                'htf': self.get_candles(instrument, tf_higher, max(50, count // 4))}

class MockDataFeed:
    def __init__(self): logger.info("Using MockDataFeed")

    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500, **kwargs) -> pd.DataFrame:
        dates = pd.date_range(end=datetime.now(), periods=count, freq='1h')
        np.random.seed(42)
        returns = np.random.randn(count) * 0.001
        close = 1.1000 * np.exp(np.cumsum(returns))
        high = close * (1 + np.abs(np.random.randn(count)) * 0.001)
        low = close * (1 - np.abs(np.random.randn(count)) * 0.001)
        return pd.DataFrame({'open': low + (high - low) * np.random.rand(count), 'high': high,
                            'low': low, 'close': close, 'volume': np.random.randint(1000, 10000, count)}, index=dates)

    def get_current_price(self, instrument: str) -> Dict[str, float]:
        return {'bid': 1.0995, 'ask': 1.1005, 'mid': 1.1000, 'spread': 0.0010}

    def get_multi_timeframe_data(self, instrument: str, tf_current: str = '1H', tf_higher: str = '4H', count: int = 500):
        return {'current': self.get_candles(instrument, tf_current, count), 'htf': self.get_candles(instrument, tf_higher, count // 4)}
