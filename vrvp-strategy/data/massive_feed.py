"""Massive API (Polygon.io) Data Feed Module"""
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict
from loguru import logger

from .massive_client import MassiveAPIClient
from .dto_transformers import MassiveDTOTransformer
from .instrument_mapper import InstrumentMapper


class MassiveDataFeed:
    """Massive API data feed using REST API and DTO normalization"""
    
    # Timeframe to Massive API parameters mapping
    TIMEFRAME_MAP = {
        '1M': (1, 'minute'),
        '5M': (5, 'minute'),
        '15M': (15, 'minute'),
        '30M': (30, 'minute'),
        '1H': (1, 'hour'),
        '4H': (4, 'hour'),
        '1D': (1, 'day'),
    }
    
    def __init__(self, api_key: str, rate_limit_per_minute: int = 5):
        """
        Args:
            api_key: Massive API key
            rate_limit_per_minute: Rate limit (default 5 for free tier)
        """
        self.client = MassiveAPIClient(api_key, rate_limit_per_minute)
        self.transformer = MassiveDTOTransformer()
        self.mapper = InstrumentMapper()
    
    def _instrument_to_ticker(self, instrument: str) -> str:
        """Convert standard instrument name to Massive API ticker"""
        return self.mapper.to_massive_ticker(instrument)
    
    def _timeframe_to_params(self, granularity: str) -> tuple:
        """Convert granularity string to (multiplier, timespan) tuple"""
        if granularity in self.TIMEFRAME_MAP:
            return self.TIMEFRAME_MAP[granularity]
        # Default to 1 hour if unknown
        logger.warning(f"Unknown granularity {granularity}, defaulting to 1H")
        return (1, 'hour')
    
    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500,
                    from_time: Optional[datetime] = None, to_time: Optional[datetime] = None) -> pd.DataFrame:
        """
        Get historical candles for an instrument.
        
        Args:
            instrument: Instrument name (e.g., 'EUR_USD')
            granularity: Timeframe (e.g., '1H', '4H', '1D')
            count: Number of candles to fetch
            from_time: Start time (optional)
            to_time: End time (optional, defaults to now)
        
        Returns:
            DataFrame with OHLCV data, indexed by timestamp
        """
        ticker = self._instrument_to_ticker(instrument)
        multiplier, timespan = self._timeframe_to_params(granularity)
        
        # Calculate date range if not provided
        if not to_time:
            to_time = datetime.now()
        if not from_time:
            # Estimate from_time based on count and granularity
            hours_map = {
                '1M': 1/60, '5M': 5/60, '15M': 15/60, '30M': 30/60,
                '1H': 1, '4H': 4, '1D': 24
            }
            hours = hours_map.get(granularity, 1) * count
            from_time = to_time - timedelta(hours=hours)
        
        # Massive API max is 50,000 candles per request
        # If we need more, we'll need to paginate (not implemented yet)
        if count > 50000:
            logger.warning(f"Requested {count} candles, but Massive API max is 50,000. Fetching 50,000.")
            count = 50000
        
        try:
            # Fetch candles from API
            logger.debug(f"Fetching candles: ticker={ticker}, multiplier={multiplier}, timespan={timespan}, from={from_time}, to={to_time}")
            raw_response = self.client.get_aggregates(
                ticker=ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_date=from_time,
                to_date=to_time,
                adjusted=True,
                sort='asc'
            )
            
            # Log raw response for debugging
            logger.info(f"API Response - Status: {raw_response.get('status')}, ResultsCount: {raw_response.get('resultsCount', 0)}")
            if raw_response.get('resultsCount', 0) > 0:
                logger.debug(f"First result keys: {raw_response.get('results', [{}])[0].keys() if raw_response.get('results') else 'No results'}")
            
            # Transform to DTOs
            candle_dtos = self.transformer.transform_candles(raw_response)
            
            # Convert DTOs to DataFrame
            data = []
            for dto in candle_dtos:
                data.append({
                    'timestamp': dto.timestamp,
                    'open': dto.open,
                    'high': dto.high,
                    'low': dto.low,
                    'close': dto.close,
                    'volume': dto.volume
                })
            
            df = pd.DataFrame(data)
            if not df.empty:
                df.set_index('timestamp', inplace=True)
                df.index = df.index.tz_localize(None)
                df = df.sort_index()
                
                # Limit to requested count (most recent)
                if len(df) > count:
                    df = df.tail(count)
            
            logger.debug(f"Fetched {len(df)} candles for {instrument} ({granularity})")
            return df
            
        except Exception as e:
            logger.error(f"Failed to get candles for {instrument}: {e}")
            return pd.DataFrame()
    
    def get_current_price(self, instrument: str) -> Dict[str, float]:
        """
        Get current bid/ask price for an instrument.
        
        Args:
            instrument: Instrument name (e.g., 'EUR_USD')
        
        Returns:
            Dict with 'bid', 'ask', 'mid', 'spread'
        """
        # Parse base and quote from instrument name
        try:
            base, quote = instrument.split('_')
        except ValueError:
            logger.error(f"Invalid instrument format: {instrument}. Expected format: BASE_QUOTE")
            return {'bid': 0.0, 'ask': 0.0, 'mid': 0.0, 'spread': 0.0}
        
        try:
            raw_response = self.client.get_last_quote(base, quote)
            price_dto = self.transformer.transform_price(raw_response)
            
            return {
                'bid': price_dto.bid,
                'ask': price_dto.ask,
                'mid': price_dto.mid,
                'spread': price_dto.spread
            }
        except Exception as e:
            logger.error(f"Failed to get current price for {instrument}: {e}")
            return {'bid': 0.0, 'ask': 0.0, 'mid': 0.0, 'spread': 0.0}
    
    def get_multi_timeframe_data(self, instrument: str, tf_current: str = '1H', 
                                 tf_higher: str = '4H', count: int = 500) -> Dict[str, pd.DataFrame]:
        """
        Get data for multiple timeframes.
        
        Args:
            instrument: Instrument name
            tf_current: Current timeframe
            tf_higher: Higher timeframe
            count: Number of candles for current timeframe
        
        Returns:
            Dict with 'current' and 'htf' DataFrames
        """
        return {
            'current': self.get_candles(instrument, tf_current, count),
            'htf': self.get_candles(instrument, tf_higher, max(50, count // 4))
        }

