"""Capital.com REST API Client with Encrypted Password Authentication"""
import base64
import time
import threading
import requests
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from loguru import logger

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    logger.warning("pycryptodome not installed. Install with: pip install pycryptodome")
    RSA = None
    PKCS1_v1_5 = None


def encrypt_password(password: str, encryption_key: str, timestamp: int) -> str:
    """
    Encrypt password using RSA with the provided encryption key from Capital.com API.

    Args:
        password: Plain text password
        encryption_key: Base64-encoded RSA public key from Capital.com
        timestamp: Timestamp from Capital.com encryptionKey endpoint

    Returns:
        Base64-encoded encrypted password string
    """
    if RSA is None or PKCS1_v1_5 is None:
        raise ImportError("pycryptodome is required for password encryption. Install with: pip install pycryptodome")

    # Combine password and timestamp
    data = f"{password}|{timestamp}"

    logger.debug(f"Data: {data}")

    # Decode the Base64 public key
    key_bytes = base64.b64decode(encryption_key)
    public_key = RSA.import_key(key_bytes)

    # Encrypt using PKCS1 v1.5
    cipher = PKCS1_v1_5.new(public_key)
    encrypted = cipher.encrypt(data.encode('utf-8'))

    # Return Base64-encoded result
    return base64.b64encode(encrypted).decode('utf-8')


class CapitalComClient:
    """
    Capital.com REST API client with encrypted password authentication.

    Handles session management, authentication, and API requests to Capital.com.
    """

    def __init__(self, api_key: str, password: str, username: str,
                 environment: str = 'demo', auto_refresh: bool = True):
        """
        Initialize the Capital.com API client.

        Args:
            api_key: Capital.com API key (X-CAP-API-KEY)
            password: Account password
            username: Account username/email
            environment: 'demo' or 'live'
            auto_refresh: Whether to automatically refresh session when expired
        """
        self.api_key = api_key
        self.password = password
        self.username = username
        self.environment = environment
        self.auto_refresh = auto_refresh

        # Set base URL based on environment
        if environment == 'live':
            self.base_url = 'https://api-capital.backend-capital.com'
        else:
            self.base_url = 'https://demo-api-capital.backend-capital.com'

        # Session tokens
        self._cst: Optional[str] = None  # Client Session Token
        self._security_token: Optional[str] = None  # X-SECURITY-TOKEN
        self._session_expires: Optional[datetime] = None

        # HTTP session for connection pooling
        self._http_session = requests.Session()
        self._lock = threading.Lock()

        # Authentication state tracking (prevent rate limiting)
        self._last_auth_attempt: Optional[datetime] = None
        self._auth_failures: int = 0
        self._max_auth_failures: int = 5
        self._min_auth_interval_seconds: int = 5  # Don't retry within 5 seconds
        self._backoff_seconds: List[int] = [5, 10, 30, 60, 120]  # Exponential backoff

        logger.info(f"Capital.com client initialized: environment={environment}, base_url={self.base_url}")

    def _get_encryption_key(self) -> Dict[str, any]:
        """
        Fetch encryption key and timestamp from Capital.com.

        Returns:
            Dict with 'encryptionKey' and 'timeStamp'
        """
        url = f"{self.base_url}/api/v1/session/encryptionKey"
        headers = {
            'X-CAP-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        }

        response = self._http_session.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        logger.debug(f"Encryption key fetched, timestamp: {data.get('timeStamp')}")
        return data

    def _should_authenticate(self) -> tuple[bool, Optional[str]]:
        """
        Determine if authentication is needed.

        Returns:
            (should_auth: bool, reason: Optional[str])
        """
        # Already authenticated with valid session?
        if self._cst and self._security_token and self._session_expires:
            if datetime.now() < self._session_expires:
                return (False, "Session still valid")

        # Too many failures?
        if self._auth_failures >= self._max_auth_failures:
            return (False, f"Max failures ({self._max_auth_failures}) reached")

        # Too soon since last attempt? (rate limit protection)
        if self._last_auth_attempt:
            seconds_since = (datetime.now() - self._last_auth_attempt).total_seconds()
            if seconds_since < self._min_auth_interval_seconds:
                return (False, f"Too soon ({seconds_since:.1f}s < {self._min_auth_interval_seconds}s)")

        return (True, None)

    def _wait_if_rate_limited(self) -> None:
        """Wait with exponential backoff if previous failures."""
        if self._auth_failures > 0:
            backoff_index = min(self._auth_failures - 1, len(self._backoff_seconds) - 1)
            delay = self._backoff_seconds[backoff_index]
            logger.warning(f"Backoff: waiting {delay}s after {self._auth_failures} failure(s)")
            time.sleep(delay)

    def authenticate(self) -> bool:
        """
        Authenticate with Capital.com using encrypted password.
        Includes smart session caching and exponential backoff on failures.

        Returns:
            True if authentication successful, False otherwise
        """
        with self._lock:
            # Check if authentication needed
            should_auth, reason = self._should_authenticate()
            if not should_auth:
                logger.debug(f"Skipping authentication: {reason}")
                return False if "Max failures" in reason else True

            # Apply exponential backoff if previous failures
            self._wait_if_rate_limited()

            # Record attempt timestamp
            self._last_auth_attempt = datetime.now()

            try:
                # Step 1: Get encryption key and timestamp
                key_data = self._get_encryption_key()
                encryption_key = key_data['encryptionKey']
                timestamp = key_data['timeStamp']

                # Step 2: Encrypt password
                encrypted_password = encrypt_password(self.password, encryption_key, timestamp)

                # Step 3: Create session
                url = f"{self.base_url}/api/v1/session"
                headers = {
                    'X-CAP-API-KEY': self.api_key,
                    'Content-Type': 'application/json'
                }
                payload = {
                    'identifier': self.username,
                    'password': encrypted_password,
                    'encryptedPassword': True
                }

                response = self._http_session.post(url, json=payload, headers=headers, timeout=30)
                response.raise_for_status()

                # Step 4: Extract session tokens from response headers
                self._cst = response.headers.get('CST')
                self._security_token = response.headers.get('X-SECURITY-TOKEN')

                # Session typically lasts 10 minutes, refresh at 8 minutes
                self._session_expires = datetime.now() + timedelta(minutes=8)

                # Reset failure counter on success
                self._auth_failures = 0

                logger.info("Successfully authenticated with Capital.com")
                logger.debug(f"Session expires at: {self._session_expires}")

                return True

            except requests.exceptions.HTTPError as e:
                self._auth_failures += 1

                # Parse error code from response
                error_code = "unknown"
                if e.response is not None:
                    try:
                        error_data = e.response.json()
                        error_code = error_data.get('errorCode', 'unknown')
                    except:
                        pass

                # Provide specific error messages
                if error_code == 'error.null.accountId':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → API key not associated with account OR environment mismatch")
                    logger.error(f"  → Check: API key valid for '{self.environment}' environment?")
                    # Permanent error - stop retrying after 3 attempts
                    if self._auth_failures >= 3:
                        self._auth_failures = self._max_auth_failures
                elif error_code == 'error.invalid.details':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → Wrong username/password")
                    logger.error("  → Check CAPITALCOM_USERNAME and CAPITALCOM_API_PASSWORD in .env")
                    # Permanent error - stop retrying after 3 attempts
                    if self._auth_failures >= 3:
                        self._auth_failures = self._max_auth_failures
                elif error_code == 'error.too-many.requests':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → Rate limited - too many authentication attempts")
                    logger.error(f"  → Will retry with exponential backoff")
                else:
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    if e.response is not None:
                        logger.error(f"  → Response: {e.response.text}")

                return False

            except Exception as e:
                self._auth_failures += 1
                logger.error(f"Authentication error: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return False

    def _ensure_authenticated(self) -> bool:
        """
        Ensure valid session exists, refreshing if needed.
        Uses cached session state to avoid redundant authentication.

        Returns:
            True if authenticated, False otherwise
        """
        # Check tokens exist AND session not expired
        if self._cst and self._security_token and self._session_expires:
            if datetime.now() < self._session_expires:
                return True  # Session still valid, no need to re-auth

            # Session expired
            if self.auto_refresh:
                logger.info("Session expired, refreshing...")
                return self.authenticate()
            else:
                logger.warning("Session expired but auto_refresh disabled")
                return False

        # No session tokens
        logger.debug("No active session, authenticating...")
        return self.authenticate()

    def _get_auth_headers(self) -> Dict[str, str]:
        """Get headers with authentication tokens."""
        return {
            'X-CAP-API-KEY': self.api_key,
            'CST': self._cst or '',
            'X-SECURITY-TOKEN': self._security_token or '',
            'Content-Type': 'application/json'
        }

    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                      json_data: Optional[Dict] = None, max_retries: int = 3) -> Dict:
        """
        Make authenticated API request with retry logic.

        Args:
            method: HTTP method ('GET', 'POST', 'PUT', 'DELETE')
            endpoint: API endpoint (e.g., '/api/v1/prices/EURUSD')
            params: Query parameters
            json_data: JSON body data
            max_retries: Maximum retry attempts

        Returns:
            JSON response as dict
        """
        if not self._ensure_authenticated():
            raise Exception("Authentication failed")

        url = f"{self.base_url}{endpoint}"
        headers = self._get_auth_headers()

        delays = [2, 5, 10]  # Retry delays in seconds

        for attempt in range(max_retries + 1):
            try:
                response = self._http_session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=30
                )

                # Handle 401 Unauthorized - re-authenticate
                if response.status_code == 401:
                    logger.warning("Session expired, re-authenticating...")
                    if self.authenticate():
                        headers = self._get_auth_headers()
                        continue
                    else:
                        raise Exception("Re-authentication failed")

                response.raise_for_status()
                return response.json()

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

    def get_server_time(self) -> Dict:
        """Get server time - useful for debugging."""
        return self._make_request('GET', '/api/v1/time')

    def get_accounts(self) -> Dict:
        """Get all accounts for the current session."""
        return self._make_request('GET', '/api/v1/accounts')

    def get_account_preferences(self) -> Dict:
        """Get account preferences."""
        return self._make_request('GET', '/api/v1/accounts/preferences')

    def get_prices(self, epic: str) -> Dict:
        """
        Get current bid/ask prices for an instrument.

        Args:
            epic: Instrument identifier (e.g., 'EURUSD')

        Returns:
            Price data with bid, ask, etc.
        """
        return self._make_request('GET', f'/api/v1/prices/{epic}')

    def get_historical_prices(self, epic: str, resolution: str = 'HOUR',
                              max_candles: int = 200, from_date: Optional[str] = None,
                              to_date: Optional[str] = None) -> Dict:
        """
        Get historical price data (candles).

        Args:
            epic: Instrument identifier (e.g., 'EURUSD')
            resolution: Timeframe - MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
            max_candles: Maximum number of candles to return (max 1000)
            from_date: Start date in ISO format (e.g., '2024-01-01T00:00:00')
            to_date: End date in ISO format

        Returns:
            Historical price data with OHLC candles
        """
        params = {
            'resolution': resolution,
            'max': min(max_candles, 1000)
        }

        if from_date:
            params['from'] = from_date
        if to_date:
            params['to'] = to_date

        return self._make_request('GET', f'/api/v1/prices/{epic}', params=params)

    def get_market_details(self, epic: str) -> Dict:
        """
        Get market details for an instrument.

        Args:
            epic: Instrument identifier

        Returns:
            Market details including trading hours, min/max sizes, etc.
        """
        return self._make_request('GET', f'/api/v1/markets/{epic}')

    def search_markets(self, search_term: str, limit: int = 20) -> Dict:
        """
        Search for markets/instruments.

        Args:
            search_term: Search query (e.g., 'EUR')
            limit: Maximum results to return

        Returns:
            List of matching markets
        """
        params = {
            'searchTerm': search_term,
            'limit': limit
        }
        return self._make_request('GET', '/api/v1/markets', params=params)

    def get_positions(self) -> Dict:
        """Get all open positions."""
        return self._make_request('GET', '/api/v1/positions')

    def get_orders(self) -> Dict:
        """Get all working orders."""
        return self._make_request('GET', '/api/v1/workingorders')

    def create_position(self, epic: str, direction: str, size: float,
                        stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None,
                        guaranteed_stop: bool = False) -> Dict:
        """
        Create a new position (market order).

        Args:
            epic: Instrument identifier
            direction: 'BUY' or 'SELL'
            size: Position size
            stop_loss: Stop loss price (optional)
            take_profit: Take profit price (optional)
            guaranteed_stop: Whether to use guaranteed stop

        Returns:
            Deal reference and status
        """
        payload = {
            'epic': epic,
            'direction': direction.upper(),
            'size': size,
            'guaranteedStop': guaranteed_stop
        }

        if stop_loss is not None:
            payload['stopLevel'] = stop_loss
        if take_profit is not None:
            payload['profitLevel'] = take_profit

        return self._make_request('POST', '/api/v1/positions', json_data=payload)

    def close_position(self, deal_id: str) -> Dict:
        """
        Close an existing position.

        Args:
            deal_id: The deal ID of the position to close

        Returns:
            Deal confirmation
        """
        return self._make_request('DELETE', f'/api/v1/positions/{deal_id}')

    def update_position(self, deal_id: str, stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None) -> Dict:
        """
        Update an existing position's stop loss or take profit.

        Args:
            deal_id: The deal ID of the position
            stop_loss: New stop loss price
            take_profit: New take profit price

        Returns:
            Updated position details
        """
        payload = {}
        if stop_loss is not None:
            payload['stopLevel'] = stop_loss
        if take_profit is not None:
            payload['profitLevel'] = take_profit

        return self._make_request('PUT', f'/api/v1/positions/{deal_id}', json_data=payload)

    def logout(self) -> bool:
        """
        Logout and invalidate the current session.

        Returns:
            True if logout successful
        """
        try:
            if self._cst and self._security_token:
                self._make_request('DELETE', '/api/v1/session')

            self._cst = None
            self._security_token = None
            self._session_expires = None

            logger.info("Successfully logged out from Capital.com")
            return True
        except Exception as e:
            logger.error(f"Logout error: {e}")
            return False

    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._cst is not None and self._security_token is not None

    def __enter__(self):
        """Context manager entry - authenticate."""
        self.authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - logout."""
        self.logout()
        return False
