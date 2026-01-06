"""Massive API (Polygon.io) REST Client with Rate Limiting"""
import requests
import threading
import time
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from loguru import logger


class RateLimiter:
    """Thread-safe token bucket rate limiter"""
    
    def __init__(self, max_tokens: int, refill_rate_per_second: float):
        """
        Args:
            max_tokens: Maximum number of tokens (e.g., 5 for free tier)
            refill_rate_per_second: Tokens refilled per second (e.g., 5/60 = 0.083 for 5/min)
        """
        self.max_tokens = max_tokens
        self.tokens = float(max_tokens)
        self.refill_rate = refill_rate_per_second
        self.last_update = time.time()
        self._lock = threading.Lock()
    
    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquire a token for an API request.
        
        Args:
            blocking: If True, wait until token is available
            timeout: Maximum time to wait (None = wait indefinitely)
        
        Returns:
            True if token acquired, False if timeout
        """
        start_time = time.time()
        
        with self._lock:
            while True:
                # Refill tokens based on elapsed time
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
                self.last_update = now
                
                # Check if token available
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                
                # If not blocking, return immediately
                if not blocking:
                    return False
                
                # Check timeout
                if timeout is not None:
                    elapsed_total = time.time() - start_time
                    if elapsed_total >= timeout:
                        return False
                
                # Wait a bit before checking again
                time.sleep(0.1)
    
    def available_tokens(self) -> int:
        """Get current number of available tokens (approximate)"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            current_tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
            return int(current_tokens)


class MassiveAPIClient:
    """Low-level REST API client for Massive API (Polygon.io)"""
    
    def __init__(self, api_key: str, rate_limit_per_minute: int = 5, base_url: str = 'https://api.polygon.io'):
        """
        Args:
            api_key: Massive API key
            rate_limit_per_minute: Rate limit (default 5 for free tier)
            base_url: Base API URL
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self._session = requests.Session()
        
        # Initialize rate limiter (with 80% buffer for safety)
        effective_rate = int(rate_limit_per_minute * 0.8)
        refill_rate = effective_rate / 60.0  # tokens per second
        self.rate_limiter = RateLimiter(effective_rate, refill_rate)
        
        logger.info(f"Massive API client initialized with rate limit: {effective_rate} req/min")
    
    def _make_request(self, endpoint: str, params: Optional[Dict] = None, max_retries: int = 3) -> Dict:
        """
        Make HTTP request with rate limiting and retry logic.
        
        Args:
            endpoint: API endpoint (e.g., '/v2/aggs/ticker/C:EURUSD/range/...')
            params: Query parameters
            max_retries: Maximum retry attempts
        
        Returns:
            JSON response as dict
        """
        url = f"{self.base_url}{endpoint}"
        
        # Add API key to params
        if params is None:
            params = {}
        params['apikey'] = self.api_key
        
        # Exponential backoff delays
        delays = [5, 10, 20]  # seconds
        
        for attempt in range(max_retries + 1):
            try:
                # Acquire rate limit token
                if not self.rate_limiter.acquire(blocking=True, timeout=60):
                    raise Exception("Rate limit token acquisition timeout")
                
                # Make request
                response = self._session.get(url, params=params, timeout=30)
                
                # Handle rate limit (429)
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', delays[min(attempt, len(delays) - 1)]))
                    logger.warning(f"Rate limit hit (429). Waiting {retry_after}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(retry_after)
                    continue
                
                # Handle other errors
                response.raise_for_status()
                
                # Parse JSON
                data = response.json()
                
                # Log response for debugging
                logger.debug(f"API Response - Status: {data.get('status')}, StatusCode: {response.status_code}")
                if 'resultsCount' in data:
                    logger.debug(f"ResultsCount: {data.get('resultsCount')}")
                if 'results' in data and len(data.get('results', [])) > 0:
                    logger.debug(f"First result sample: {list(data['results'][0].keys())}")
                
                # Check for API-level errors
                if data.get('status') == 'ERROR':
                    error_msg = data.get('error', 'Unknown error')
                    logger.error(f"API error response: {data}")
                    raise Exception(f"API error: {error_msg}")
                
                return data
                
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(f"Request timeout. Retrying in {delay}s... ({attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise
            
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(f"Request failed: {e}. Retrying in {delay}s... ({attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise
        
        raise Exception(f"Request failed after {max_retries} retries")
    
    def get_aggregates(self, ticker: str, multiplier: int, timespan: str, 
                       from_date: datetime, to_date: datetime, 
                       adjusted: bool = True, sort: str = 'asc') -> Dict:
        """
        Get historical aggregate (candle) data.
        
        Args:
            ticker: Ticker symbol (e.g., 'C:EURUSD')
            multiplier: Size of timespan (e.g., 1)
            timespan: Timespan unit ('minute', 'hour', 'day')
            from_date: Start date
            to_date: End date
            adjusted: Whether results are adjusted for splits
            sort: Sort order ('asc' or 'desc')
        
        Returns:
            API response with 'results' array containing candles
        """
        # Format dates as YYYY-MM-DD
        from_str = from_date.strftime('%Y-%m-%d')
        to_str = to_date.strftime('%Y-%m-%d')
        
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_str}/{to_str}"
        params = {
            'adjusted': 'true' if adjusted else 'false',
            'sort': sort
        }
        
        logger.debug(f"Fetching aggregates: {ticker} {multiplier}{timespan} from {from_str} to {to_str}")
        return self._make_request(endpoint, params)
    
    def get_last_quote(self, from_currency: str, to_currency: str) -> Dict:
        """
        Get last quote (bid/ask) for a currency pair.
        
        Args:
            from_currency: Base currency (e.g., 'EUR')
            to_currency: Quote currency (e.g., 'USD')
        
        Returns:
            API response with 'last' object containing bid/ask
        """
        endpoint = f"/v1/last_quote/currencies/{from_currency}/{to_currency}"
        
        logger.debug(f"Fetching last quote: {from_currency}/{to_currency}")
        return self._make_request(endpoint)
    
    def get_forex_tickers(self, active: bool = True) -> Dict:
        """
        Get list of available forex tickers.
        
        Args:
            active: Only return active tickers
        
        Returns:
            API response with 'results' array containing tickers
        """
        endpoint = "/v3/reference/tickers"
        params = {
            'market': 'fx',
            'active': 'true' if active else 'false'
        }
        
        logger.debug("Fetching forex tickers")
        return self._make_request(endpoint, params)
    
    def check_rate_limit(self) -> Dict[str, int]:
        """Check current rate limit status"""
        available = self.rate_limiter.available_tokens()
        return {
            'available_tokens': available,
            'max_tokens': self.rate_limiter.max_tokens
        }
