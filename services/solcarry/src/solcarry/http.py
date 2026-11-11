"""HTTP client: timeouts, retries with backoff, per-host concurrency cap, user agent."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import HttpCfg

RETRY_STATUS = {429, 500, 502, 503, 504}


class RetryableStatus(Exception):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableStatus, httpx.TimeoutException, httpx.TransportError))


class Http:
    """Thin sync wrapper. 4xx (other than 429) is returned to the caller, never raised."""

    def __init__(
        self,
        cfg: HttpCfg | None = None,
        client: httpx.Client | None = None,
        max_attempts: int = 5,
        backoff_max_s: float = 30.0,
    ) -> None:
        self.cfg = cfg or HttpCfg()
        self.client = client or httpx.Client(
            timeout=self.cfg.timeout_s,
            headers={"User-Agent": self.cfg.user_agent},
            follow_redirects=True,
        )
        self._locks: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(self.cfg.max_parallel_per_host)
        )
        self._guard = threading.Lock()
        self._max_attempts = max_attempts
        self._backoff_max = backoff_max_s

    def _sem(self, url: str) -> threading.Semaphore:
        host = urlparse(url).netloc
        with self._guard:
            return self._locks[host]

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, max=self._backoff_max),
            reraise=True,
        )
        def _do() -> httpx.Response:
            with self._sem(url):
                r = self.client.get(url, params=params)
            if r.status_code in RETRY_STATUS:
                raise RetryableStatus(r.status_code, url)
            return r

        return _do()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Http:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
