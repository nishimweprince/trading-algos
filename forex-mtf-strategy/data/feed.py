"""
OANDA Data Feed for live and historical data retrieval.

Provides streaming and polling access to OANDA's pricing data.
"""

import time
from datetime import datetime, timedelta
from typing import Generator, Optional

import pandas as pd
from oandapyV20 import API
from oandapyV20.endpoints import instruments, pricing
from oandapyV20.exceptions import V20Error

from config.settings import get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)


class OANDADataFeed:
    """OANDA data feed for forex price data."""
    
    # Granularity mapping
    GRANULARITY_MAP = {
        "M1": "M1",
        "M5": "M5",
        "M15": "M15",
        "M30": "M30",
        "H1": "H1",
        "H4": "H4",
        "D": "D",
        "W": "W",
        "M": "M",
    }
    
    def __init__(self, access_token: Optional[str] = None, account_id: Optional[str] = None):
        """
        Initialize OANDA data feed.
        
        Args:
            access_token: OANDA API access token (uses settings if not provided)
            account_id: OANDA account ID (uses settings if not provided)
        """
        settings = get_settings()
        self.access_token = access_token or settings.oanda.access_token
        self.account_id = account_id or settings.oanda.account_id
        self.environment = settings.oanda.environment
        
        if not self.access_token:
            logger.warning("OANDA access token not configured. Using demo mode.")
            self._api = None
        else:
            self._api = API(access_token=self.access_token, environment=self.environment)
    
    def get_candles(
        self,
        instrument: str,
        granularity: str = "H1",
        count: Optional[int] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical candle data from OANDA.
        
        Args:
            instrument: Instrument name (e.g., "EUR_USD")
            granularity: Candle granularity (M1, M5, M15, M30, H1, H4, D, W, M)
            count: Number of candles to fetch (max 5000)
            from_time: Start datetime
            to_time: End datetime
            
        Returns:
            DataFrame with columns: open, high, low, close, volume, complete
        """
        if self._api is None:
            logger.error("OANDA API not configured. Cannot fetch candles.")
            return pd.DataFrame()
        
        params = {
            "granularity": self.GRANULARITY_MAP.get(granularity, granularity),
            "price": "M",  # Midpoint prices
        }
        
        if count:
            params["count"] = min(count, 5000)
        
        if from_time:
            params["from"] = from_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        if to_time:
            params["to"] = to_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        try:
            request = instruments.InstrumentsCandles(instrument=instrument, params=params)
            response = self._api.request(request)
            
            candles = response.get("candles", [])
            if not candles:
                return pd.DataFrame()
            
            data = []
            for candle in candles:
                mid = candle["mid"]
                data.append({
                    "time": pd.to_datetime(candle["time"]),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(candle["volume"]),
                    "complete": candle["complete"],
                })
            
            df = pd.DataFrame(data)
            df.set_index("time", inplace=True)
            df.index = df.index.tz_localize(None)  # Remove timezone for consistency
            
            logger.info(f"Fetched {len(df)} candles for {instrument} ({granularity})")
            return df
            
        except V20Error as e:
            logger.error(f"OANDA API error: {e}")
            return pd.DataFrame()
    
    def get_candles_range(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
    ) -> pd.DataFrame:
        """
        Fetch candles over a date range, handling OANDA's 5000 candle limit.
        
        Args:
            instrument: Instrument name
            granularity: Candle granularity
            from_time: Start datetime
            to_time: End datetime
            
        Returns:
            DataFrame with all candles in the range
        """
        all_candles = []
        current_from = from_time
        
        # Granularity to timedelta mapping for pagination
        granularity_td = {
            "M1": timedelta(minutes=1),
            "M5": timedelta(minutes=5),
            "M15": timedelta(minutes=15),
            "M30": timedelta(minutes=30),
            "H1": timedelta(hours=1),
            "H4": timedelta(hours=4),
            "D": timedelta(days=1),
            "W": timedelta(weeks=1),
            "M": timedelta(days=30),
        }
        
        td = granularity_td.get(granularity, timedelta(hours=1))
        batch_size = 5000
        
        while current_from < to_time:
            batch_to = min(current_from + td * batch_size, to_time)
            
            df = self.get_candles(
                instrument=instrument,
                granularity=granularity,
                from_time=current_from,
                to_time=batch_to,
            )
            
            if df.empty:
                break
            
            all_candles.append(df)
            current_from = df.index[-1] + td
            
            # Rate limiting
            time.sleep(0.1)
        
        if not all_candles:
            return pd.DataFrame()
        
        result = pd.concat(all_candles)
        result = result[~result.index.duplicated(keep="last")]
        return result.sort_index()
    
    def get_current_price(self, instrument: str) -> Optional[dict]:
        """
        Get current bid/ask prices for an instrument.
        
        Args:
            instrument: Instrument name
            
        Returns:
            Dict with 'bid', 'ask', 'mid', 'time' or None if unavailable
        """
        if self._api is None:
            logger.error("OANDA API not configured.")
            return None
        
        try:
            params = {"instruments": instrument}
            request = pricing.PricingInfo(accountID=self.account_id, params=params)
            response = self._api.request(request)
            
            prices = response.get("prices", [])
            if not prices:
                return None
            
            price = prices[0]
            bid = float(price["bids"][0]["price"])
            ask = float(price["asks"][0]["price"])
            
            return {
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
                "time": pd.to_datetime(price["time"]),
                "tradeable": price["tradeable"],
            }
            
        except V20Error as e:
            logger.error(f"Error fetching current price: {e}")
            return None
    
    def stream_prices(self, instruments: list[str]) -> Generator[dict, None, None]:
        """
        Stream live prices for multiple instruments.
        
        Args:
            instruments: List of instrument names
            
        Yields:
            Price update dicts with instrument, bid, ask, time
        """
        if self._api is None:
            logger.error("OANDA API not configured for streaming.")
            return
        
        from oandapyV20.endpoints.pricing import PricingStream
        
        params = {"instruments": ",".join(instruments)}
        
        try:
            request = PricingStream(accountID=self.account_id, params=params)
            
            for response in self._api.request(request):
                if response["type"] == "PRICE":
                    yield {
                        "instrument": response["instrument"],
                        "bid": float(response["bids"][0]["price"]),
                        "ask": float(response["asks"][0]["price"]),
                        "time": pd.to_datetime(response["time"]),
                    }
                elif response["type"] == "HEARTBEAT":
                    logger.debug("Heartbeat received")
                    
        except V20Error as e:
            logger.error(f"Streaming error: {e}")
