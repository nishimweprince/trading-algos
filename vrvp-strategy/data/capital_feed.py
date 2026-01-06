"""Capital.com Data Feed Module for Market Data"""
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict
from loguru import logger

from .capital_client import CapitalComClient
from .dto_transformers import CapitalComDTOTransformer
from .instrument_mapper import InstrumentMapper


class CapitalDataFeed:
    """Capital.com data feed using REST API and DTO normalization"""

    # Timeframe to Capital.com resolution mapping
    TIMEFRAME_MAP = {
        '1M': 'MINUTE',
        '5M': 'MINUTE_5',
        '15M': 'MINUTE_15',
        '30M': 'MINUTE_30',
        '1H': 'HOUR',
        '4H': 'HOUR_4',
        '1D': 'DAY',
        '1W': 'WEEK',
    }

    def __init__(self, api_key: str, password: str, username: str,
                 environment: str = 'demo'):
        """
        Initialize Capital.com data feed.

        Args:
            api_key: Capital.com API key
            password: Account password
            username: Account username/email
            environment: 'demo' or 'live'
        """
        self.client = CapitalComClient(
            api_key=api_key,
            password=password,
            username=username,
            environment=environment,
            auto_refresh=True
        )
        self.transformer = CapitalComDTOTransformer()
        self.mapper = InstrumentMapper()
        self._authenticated = False

    def _ensure_authenticated(self) -> bool:
        """Ensure client is authenticated."""
        if not self._authenticated or not self.client.is_authenticated:
            self._authenticated = self.client.authenticate()
        return self._authenticated

    def _instrument_to_epic(self, instrument: str) -> str:
        """Convert standard instrument name to Capital.com epic."""
        return self.mapper.to_capitalcom_epic(instrument)

    def _timeframe_to_resolution(self, granularity: str) -> str:
        """Convert granularity string to Capital.com resolution."""
        if granularity in self.TIMEFRAME_MAP:
            return self.TIMEFRAME_MAP[granularity]
        logger.warning(f"Unknown granularity {granularity}, defaulting to HOUR")
        return 'HOUR'

    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500,
                    from_time: Optional[datetime] = None, to_time: Optional[datetime] = None) -> pd.DataFrame:
        """
        Get historical candles for an instrument.

        Args:
            instrument: Instrument name (e.g., 'EUR_USD')
            granularity: Timeframe (e.g., '1H', '4H', '1D')
            count: Number of candles to fetch (max 1000)
            from_time: Start time (optional)
            to_time: End time (optional, defaults to now)

        Returns:
            DataFrame with OHLCV data, indexed by timestamp
        """
        if not self._ensure_authenticated():
            logger.error("Failed to authenticate with Capital.com")
            return pd.DataFrame()

        epic = self._instrument_to_epic(instrument)
        resolution = self._timeframe_to_resolution(granularity)

        # Calculate date range if not provided
        if to_time is None:
            to_time = datetime.now()

        try:
            # Build request parameters
            params = {
                'resolution': resolution,
                'max': min(count, 1000)
            }

            if from_time:
                params['from_date'] = from_time.strftime('%Y-%m-%dT%H:%M:%S')
            if to_time:
                params['to_date'] = to_time.strftime('%Y-%m-%dT%H:%M:%S')

            # Fetch candles from API
            logger.debug(f"Fetching candles: epic={epic}, resolution={resolution}, count={count}")
            raw_response = self.client.get_historical_prices(
                epic=epic,
                resolution=resolution,
                max_candles=min(count, 1000),
                from_date=params.get('from_date'),
                to_date=params.get('to_date')
            )

            # Log response for debugging
            prices_count = len(raw_response.get('prices', []))
            logger.info(f"API Response - Prices count: {prices_count}")

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
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    def get_current_price(self, instrument: str) -> Dict[str, float]:
        """
        Get current bid/ask price for an instrument.

        Args:
            instrument: Instrument name (e.g., 'EUR_USD')

        Returns:
            Dict with 'bid', 'ask', 'mid', 'spread'
        """
        if not self._ensure_authenticated():
            logger.error("Failed to authenticate with Capital.com")
            return {'bid': 0.0, 'ask': 0.0, 'mid': 0.0, 'spread': 0.0}

        epic = self._instrument_to_epic(instrument)

        try:
            raw_response = self.client.get_prices(epic)
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

    def get_account_info(self) -> Dict:
        """Get account information."""
        if not self._ensure_authenticated():
            logger.error("Failed to authenticate with Capital.com")
            return {}

        try:
            return self.client.get_accounts()
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return {}

    def check_connection(self) -> bool:
        """Check if connection to Capital.com is working."""
        try:
            if not self._ensure_authenticated():
                return False

            # Try to get server time as a simple connectivity check
            self.client.get_server_time()
            return True
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False

    def logout(self) -> None:
        """Logout from Capital.com session."""
        if self.client.is_authenticated:
            self.client.logout()
            self._authenticated = False
