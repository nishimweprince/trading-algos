"""Capital.com REST API client with encrypted password authentication."""
import base64
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
    """Encrypt password using RSA with the public key returned by Capital.com.

    Combines `password|timestamp`, encrypts under PKCS1_v1_5, returns base64.
    """
    if RSA is None or PKCS1_v1_5 is None:
        raise ImportError("pycryptodome is required for password encryption.")

    data = f"{password}|{timestamp}"
    key_bytes = base64.b64decode(encryption_key)
    public_key = RSA.import_key(key_bytes)
    cipher = PKCS1_v1_5.new(public_key)
    encrypted = cipher.encrypt(data.encode('utf-8'))
    return base64.b64encode(encrypted).decode('utf-8')


class CapitalComClient:
    """Capital.com REST API client with encrypted password auth, session caching, and backoff."""

    def __init__(self, api_key: str, password: str, username: str,
                 environment: str = 'demo', auto_refresh: bool = True):
        self.api_key = api_key
        self.password = password
        self.username = username
        self.environment = environment
        self.auto_refresh = auto_refresh

        if environment == 'live':
            self.base_url = 'https://api-capital.backend-capital.com'
        else:
            self.base_url = 'https://demo-api-capital.backend-capital.com'

        self._cst: Optional[str] = None
        self._security_token: Optional[str] = None
        self._session_expires: Optional[datetime] = None

        self._http_session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._http_session.mount("https://", adapter)
        self._lock = threading.Lock()

        self._last_auth_attempt: Optional[datetime] = None
        self._auth_failures: int = 0
        self._max_auth_failures: int = 5
        self._min_auth_interval_seconds: int = 5
        self._backoff_seconds: List[int] = [5, 10, 30, 60, 120]

        logger.info(f"Capital.com client initialized: environment={environment}, base_url={self.base_url}")

    def _get_encryption_key(self) -> Dict[str, any]:
        url = f"{self.base_url}/api/v1/session/encryptionKey"
        headers = {'X-CAP-API-KEY': self.api_key, 'Content-Type': 'application/json'}
        response = self._http_session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Encryption key fetched, timestamp: {data.get('timeStamp')}")
        return data

    def _should_authenticate(self) -> tuple[bool, Optional[str]]:
        if self._cst and self._security_token and self._session_expires:
            if datetime.now() < self._session_expires:
                return (False, "Session still valid")

        if self._auth_failures >= self._max_auth_failures:
            return (False, f"Max failures ({self._max_auth_failures}) reached")

        if self._last_auth_attempt:
            seconds_since = (datetime.now() - self._last_auth_attempt).total_seconds()
            if seconds_since < self._min_auth_interval_seconds:
                return (False, f"Too soon ({seconds_since:.1f}s < {self._min_auth_interval_seconds}s)")

        return (True, None)

    def _wait_if_rate_limited(self) -> None:
        if self._auth_failures > 0:
            backoff_index = min(self._auth_failures - 1, len(self._backoff_seconds) - 1)
            delay = self._backoff_seconds[backoff_index]
            logger.warning(f"Backoff: waiting {delay}s after {self._auth_failures} failure(s)")
            time.sleep(delay)

    def authenticate(self) -> bool:
        with self._lock:
            should_auth, reason = self._should_authenticate()
            if not should_auth:
                logger.debug(f"Skipping authentication: {reason}")
                return False if "Max failures" in reason else True

            self._wait_if_rate_limited()
            self._last_auth_attempt = datetime.now()

            try:
                key_data = self._get_encryption_key()
                encryption_key = key_data['encryptionKey']
                timestamp = key_data['timeStamp']

                encrypted_password = encrypt_password(self.password, encryption_key, timestamp)

                url = f"{self.base_url}/api/v1/session"
                headers = {'X-CAP-API-KEY': self.api_key, 'Content-Type': 'application/json'}
                payload = {
                    'identifier': self.username,
                    'password': encrypted_password,
                    'encryptedPassword': True,
                }

                response = self._http_session.post(url, json=payload, headers=headers, timeout=30)
                response.raise_for_status()

                self._cst = response.headers.get('CST')
                self._security_token = response.headers.get('X-SECURITY-TOKEN')
                self._session_expires = datetime.now() + timedelta(minutes=8)
                self._auth_failures = 0

                logger.info("Successfully authenticated with Capital.com")
                logger.debug(f"Session expires at: {self._session_expires}")
                return True

            except requests.exceptions.HTTPError as e:
                self._auth_failures += 1
                error_code = "unknown"
                if e.response is not None:
                    try:
                        error_code = e.response.json().get('errorCode', 'unknown')
                    except Exception:
                        pass

                if error_code == 'error.null.accountId':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → API key not associated with account OR environment mismatch")
                    logger.error(f"  → Check: API key valid for '{self.environment}' environment?")
                    if self._auth_failures >= 3:
                        self._auth_failures = self._max_auth_failures
                elif error_code == 'error.invalid.details':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → Wrong username/password")
                    if self._auth_failures >= 3:
                        self._auth_failures = self._max_auth_failures
                elif error_code == 'error.too-many.requests':
                    logger.error(f"Authentication failed ({e.response.status_code}): {error_code}")
                    logger.error("  → Rate limited; will retry with exponential backoff")
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
        if self._cst and self._security_token and self._session_expires:
            if datetime.now() < self._session_expires:
                return True
            if self.auto_refresh:
                logger.info("Session expired, refreshing...")
                return self.authenticate()
            logger.warning("Session expired but auto_refresh disabled")
            return False

        logger.debug("No active session, authenticating...")
        return self.authenticate()

    def _get_auth_headers(self) -> Dict[str, str]:
        return {
            'X-CAP-API-KEY': self.api_key,
            'CST': self._cst or '',
            'X-SECURITY-TOKEN': self._security_token or '',
            'Content-Type': 'application/json',
        }

    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                      json_data: Optional[Dict] = None, max_retries: int = 3) -> Dict:
        if not self._ensure_authenticated():
            raise Exception("Authentication failed")

        url = f"{self.base_url}{endpoint}"
        headers = self._get_auth_headers()
        delays = [2, 5, 10]

        for attempt in range(max_retries + 1):
            try:
                response = self._http_session.request(
                    method=method, url=url, headers=headers,
                    params=params, json=json_data, timeout=30,
                )

                if response.status_code == 401:
                    logger.warning("Session expired, re-authenticating...")
                    if self.authenticate():
                        headers = self._get_auth_headers()
                        continue
                    raise Exception("Re-authentication failed")

                if 400 <= response.status_code < 500:
                    error_text = response.text
                    try:
                        error_json = response.json()
                        error_code = error_json.get('errorCode', 'UNKNOWN')
                        error_message = error_json.get('errorMessage', error_text)
                        logger.error(
                            f"Client error {response.status_code} on {endpoint}: "
                            f"{error_code} - {error_message}"
                        )
                    except Exception:
                        logger.error(f"Client error {response.status_code} on {endpoint}: {error_text}")
                    response.raise_for_status()

                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(f"Request timeout. Retrying in {delay}s... ({attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status is not None and 400 <= status < 500 and status != 401:
                    raise
                if attempt < max_retries:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(f"Request failed: {e}. Retrying in {delay}s... ({attempt + 1}/{max_retries})")
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
        return self._make_request('GET', '/api/v1/time')

    def get_accounts(self) -> Dict:
        return self._make_request('GET', '/api/v1/accounts')

    def get_account_preferences(self) -> Dict:
        return self._make_request('GET', '/api/v1/accounts/preferences')

    def get_account_balance(self) -> Dict:
        data = self.get_accounts()
        accounts = data.get('accounts', [])
        if not accounts:
            logger.warning("No accounts found for the current session")
            return {'balance': 0.0, 'profitLoss': 0.0, 'equity': 0.0}

        main_account = accounts[0]
        balance_obj = main_account.get('balance', {})
        balance = float(balance_obj.get('balance', 0.0))
        profit_loss = float(balance_obj.get('profitLoss', 0.0))
        equity = balance + profit_loss

        return {
            'balance': balance,
            'profitLoss': profit_loss,
            'equity': equity,
            'available': float(balance_obj.get('available', 0.0)),
            'deposit': float(balance_obj.get('deposit', 0.0)),
            'currency': main_account.get('currency', 'USD'),
        }

    def get_prices(self, epic: str) -> Dict:
        return self._make_request('GET', f'/api/v1/prices/{epic}')

    def get_historical_prices(self, epic: str, resolution: str = 'HOUR',
                              max_candles: int = 200, from_date: Optional[str] = None,
                              to_date: Optional[str] = None) -> Dict:
        params = {'resolution': resolution, 'max': min(max_candles, 1000)}
        if from_date:
            params['from'] = from_date
        if to_date:
            params['to'] = to_date
        return self._make_request('GET', f'/api/v1/prices/{epic}', params=params)

    def get_market_details(self, epic: str) -> Dict:
        return self._make_request('GET', f'/api/v1/markets/{epic}')

    def search_markets(self, search_term: str, limit: int = 20) -> Dict:
        params = {'searchTerm': search_term, 'limit': limit}
        return self._make_request('GET', '/api/v1/markets', params=params)

    def get_positions(self) -> Dict:
        return self._make_request('GET', '/api/v1/positions')

    def get_orders(self) -> Dict:
        return self._make_request('GET', '/api/v1/workingorders')

    def create_position(self, epic: str, direction: str, size: float,
                        stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None,
                        guaranteed_stop: bool = False) -> Dict:
        payload = {'epic': epic, 'direction': direction.upper(), 'size': size}
        if guaranteed_stop:
            payload['guaranteedStop'] = True
        if stop_loss is not None:
            payload['stopLevel'] = stop_loss
        if take_profit is not None:
            payload['profitLevel'] = take_profit

        logger.info(
            "Creating position payload: "
            f"epic={payload['epic']}, direction={payload['direction']}, "
            f"size={payload['size']}, stopLevel={payload.get('stopLevel')}, "
            f"profitLevel={payload.get('profitLevel')}, guaranteedStop={payload.get('guaranteedStop', False)}"
        )
        return self._make_request('POST', '/api/v1/positions', json_data=payload)

    def close_position(self, deal_id: str) -> Dict:
        return self._make_request('DELETE', f'/api/v1/positions/{deal_id}')

    def update_position(self, deal_id: str, stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None) -> Dict:
        payload = {}
        if stop_loss is not None:
            payload['stopLevel'] = stop_loss
        if take_profit is not None:
            payload['profitLevel'] = take_profit
        return self._make_request('PUT', f'/api/v1/positions/{deal_id}', json_data=payload)

    def logout(self) -> bool:
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
        return self._cst is not None and self._security_token is not None

    def __enter__(self):
        self.authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()
        return False
