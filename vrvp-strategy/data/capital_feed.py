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

    def authenticate(self) -> bool:
        """
        Explicitly authenticate with Capital.com.
        Call this BEFORE starting data fetching operations.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Authenticating Capital.com data feed...")
        success = self.client.authenticate()
        if success:
            self._authenticated = True
            logger.info("✓ Data feed authenticated successfully")
        else:
            self._authenticated = False
            logger.error("✗ Data feed authentication failed")
        return success

    @property
    def is_authenticated(self) -> bool:
        """Check if feed is currently authenticated."""
        return self._authenticated and self.client.is_authenticated

    def _ensure_authenticated(self) -> bool:
        """
        Ensure client authenticated (called before each data request).
        Uses cached state to avoid redundant calls.
        """
        # Check if we think we're authenticated AND client confirms AND session valid
        if self._authenticated and self.client.is_authenticated:
            if self.client._session_expires and datetime.now() < self.client._session_expires:
                return True  # All good, use cached session

        # Session invalid or expired, re-authenticate
        logger.debug("Session invalid, re-authenticating...")
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

        try:
            # Build request parameters
            # Note: Capital.com API doesn't require date parameters for most recent candles
            params = {
                'resolution': resolution,
                'max': min(count, 1000)
            }

            # Only add date parameters if explicitly provided
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
            error_msg = str(e)
            logger.error(f"Failed to get candles for {instrument} with epic {epic}: {e}")
            
            # If 404 error, suggest searching for the correct epic
            if '404' in error_msg or 'Not Found' in error_msg:
                logger.warning(f"Epic '{epic}' not found. The instrument '{instrument}' may not be available or may use a different epic format.")
                logger.info(f"Try searching for markets with: feed.search_markets('{instrument.split('_')[0] if '_' in instrument else instrument}')")
                logger.info(f"Or check Capital.com platform for the correct epic format for {instrument}")
            
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

    def get_equity(self) -> float:
        """Get current account equity."""
        if not self._ensure_authenticated():
            return 0.0
            
        try:
            balance_data = self.client.get_account_balance()
            return balance_data.get('equity', 0.0)
        except Exception as e:
            logger.error(f"Failed to get equity: {e}")
            return 0.0

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

    def search_markets(self, search_term: str, limit: int = 20) -> Dict:
        """
        Search for markets/instruments on Capital.com.
        
        Useful for finding the correct epic format for instruments not in the mapper.
        
        Args:
            search_term: Search query (e.g., 'XAU', 'GOLD', 'EUR')
            limit: Maximum results to return
            
        Returns:
            Dict with list of matching markets, each containing 'epic', 'instrumentName', etc.
        """
        if not self._ensure_authenticated():
            logger.error("Failed to authenticate with Capital.com")
            return {}
        
        try:
            result = self.client.search_markets(search_term, limit=limit)
            markets = result.get('markets', [])
            logger.info(f"Found {len(markets)} markets matching '{search_term}'")
            for market in markets[:5]:  # Show first 5
                epic = market.get('epic', 'N/A')
                name = market.get('instrumentName', 'N/A')
                logger.info(f"  - {name}: epic='{epic}'")
            return result
        except Exception as e:
            logger.error(f"Failed to search markets: {e}")
            return {}
    
    def logout(self) -> None:
        """Logout from Capital.com session."""
        if self.client.is_authenticated:
            self.client.logout()
            self._authenticated = False
