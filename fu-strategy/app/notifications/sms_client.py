"""Pindo SMS API client."""
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import Settings


class SMSError(Exception):
    """Raised when Pindo transport or API sending fails."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[Any] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class SMSClient:
    """Async client for Pindo SMS sends."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self) -> Dict[str, str]:
        return {
            'Authorization': f"Bearer {self.settings.pindo_token}",
            'Content-Type': 'application/json',
        }

    def _ensure_configured(self) -> None:
        if not self.settings.sms_configured:
            raise SMSError("SMS not configured: set PINDO_TOKEN in environment.")

    async def send_text(self, to: str, text: str, sender: Optional[str] = None) -> Dict[str, Any]:
        self._ensure_configured()
        payload = {
            'to': to,
            'text': text,
            'sender': sender or self.settings.pindo_sender_id,
        }
        client = await self._http()
        try:
            response = await client.post(
                self.settings.pindo_api_url,
                headers=self._auth_headers(),
                json=payload,
            )
        except httpx.HTTPError as e:
            logger.error(f"SMS transport error: {e}")
            raise SMSError(f"Transport error: {e}") from e

        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except Exception:
                body = response.text
            logger.error(f"SMS API error {response.status_code}: {body}")
            raise SMSError(
                f"SMS API returned {response.status_code}",
                status_code=response.status_code,
                response_body=body,
            )

        return response.json()
