"""Capital.com data feed — high-level wrapper over CapitalComClient with DTO normalization."""
import pandas as pd
from datetime import datetime
from typing import Optional, Dict
from loguru import logger

from app.data.providers.capital_com import CapitalComClient
from app.data.dto_transformers import CapitalComDTOTransformer
from app.data.instrument_mapper import InstrumentMapper


class CapitalDataFeed:
    """Capital.com data feed using REST + DTO normalization."""

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

    def __init__(self, api_key: str, password: str, username: str, environment: str = 'demo'):
        self.client = CapitalComClient(
            api_key=api_key,
            password=password,
            username=username,
            environment=environment,
            auto_refresh=True,
        )
        self.transformer = CapitalComDTOTransformer()
        self.mapper = InstrumentMapper()
        self._authenticated = False

    def authenticate(self) -> bool:
        logger.info("Authenticating Capital.com data feed...")
        success = self.client.authenticate()
        if success:
            self._authenticated = True
            logger.info("Data feed authenticated successfully")
        else:
            self._authenticated = False
            logger.error("Data feed authentication failed")
        return success

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self.client.is_authenticated

    def _ensure_authenticated(self) -> bool:
        if self._authenticated and self.client.is_authenticated:
            if self.client._session_expires and datetime.now() < self.client._session_expires:
                return True
        logger.debug("Session invalid, re-authenticating...")
        self._authenticated = self.client.authenticate()
        return self._authenticated

    def _instrument_to_epic(self, instrument: str) -> str:
        return self.mapper.to_capitalcom_epic(instrument)

    def _timeframe_to_resolution(self, granularity: str) -> str:
        if granularity in self.TIMEFRAME_MAP:
            return self.TIMEFRAME_MAP[granularity]
        logger.warning(f"Unknown granularity {granularity}, defaulting to HOUR")
        return 'HOUR'

    def get_candles(self, instrument: str, granularity: str = '1H', count: int = 500,
                    from_time: Optional[datetime] = None, to_time: Optional[datetime] = None) -> pd.DataFrame:
        """Fetch historical candles. Returns DataFrame indexed by timestamp."""
        if not self._ensure_authenticated():
            logger.error("Failed to authenticate with Capital.com")
            return pd.DataFrame()

        epic = self._instrument_to_epic(instrument)
        resolution = self._timeframe_to_resolution(granularity)

        try:
            from_date = from_time.strftime('%Y-%m-%dT%H:%M:%S') if from_time else None
            to_date = to_time.strftime('%Y-%m-%dT%H:%M:%S') if to_time else None

            logger.debug(f"Fetching candles: epic={epic}, resolution={resolution}, count={count}")
            raw_response = self.client.get_historical_prices(
                epic=epic,
                resolution=resolution,
                max_candles=min(count, 1000),
                from_date=from_date,
                to_date=to_date,
            )

            prices_count = len(raw_response.get('prices', []))
            logger.info(f"API Response - Prices count: {prices_count}")

            candle_dtos = self.transformer.transform_candles(raw_response)
            data = [{
                'timestamp': dto.timestamp,
                'open': dto.open,
                'high': dto.high,
                'low': dto.low,
                'close': dto.close,
                'volume': dto.volume,
            } for dto in candle_dtos]

            df = pd.DataFrame(data)
            if not df.empty:
                df.set_index('timestamp', inplace=True)
                df.index = df.index.tz_localize(None)
                df = df.sort_index()
                if len(df) > count:
                    df = df.tail(count)

            logger.debug(f"Fetched {len(df)} candles for {instrument} ({granularity})")
            return df

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to get candles for {instrument} with epic {epic}: {e}")
            if '404' in error_msg or 'Not Found' in error_msg:
                logger.warning(f"Epic '{epic}' not found for instrument '{instrument}'.")
                base = instrument.split('_')[0] if '_' in instrument else instrument
                logger.info(f"Try search_markets('{base}') to find the correct epic.")
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    def get_current_price(self, instrument: str) -> Dict[str, float]:
        if not self._ensure_authenticated():
            return {'bid': 0.0, 'ask': 0.0, 'mid': 0.0, 'spread': 0.0}

        epic = self._instrument_to_epic(instrument)
        try:
            raw_response = self.client.get_prices(epic)
            price_dto = self.transformer.transform_price(raw_response)
            return {
                'bid': price_dto.bid,
                'ask': price_dto.ask,
                'mid': price_dto.mid,
                'spread': price_dto.spread,
            }
        except Exception as e:
            logger.error(f"Failed to get current price for {instrument}: {e}")
            return {'bid': 0.0, 'ask': 0.0, 'mid': 0.0, 'spread': 0.0}

    def get_multi_timeframe_data(self, instrument: str, tf_current: str = '1H',
                                 tf_higher: str = '4H', count: int = 500) -> Dict[str, pd.DataFrame]:
        return {
            'current': self.get_candles(instrument, tf_current, count),
            'htf': self.get_candles(instrument, tf_higher, max(50, count // 4)),
        }

    def get_account_info(self) -> Dict:
        if not self._ensure_authenticated():
            return {}
        try:
            return self.client.get_accounts()
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return {}

    def get_equity(self) -> float:
        if not self._ensure_authenticated():
            return 0.0
        try:
            return self.client.get_account_balance().get('equity', 0.0)
        except Exception as e:
            logger.error(f"Failed to get equity: {e}")
            return 0.0

    def check_connection(self) -> bool:
        try:
            if not self._ensure_authenticated():
                return False
            self.client.get_server_time()
            return True
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False

    def search_markets(self, search_term: str, limit: int = 20) -> Dict:
        if not self._ensure_authenticated():
            return {}
        try:
            result = self.client.search_markets(search_term, limit=limit)
            markets = result.get('markets', [])
            logger.info(f"Found {len(markets)} markets matching '{search_term}'")
            for market in markets[:5]:
                logger.info(f"  - {market.get('instrumentName', 'N/A')}: epic='{market.get('epic', 'N/A')}'")
            return result
        except Exception as e:
            logger.error(f"Failed to search markets: {e}")
            return {}

    def logout(self) -> None:
        if self.client.is_authenticated:
            self.client.logout()
            self._authenticated = False
