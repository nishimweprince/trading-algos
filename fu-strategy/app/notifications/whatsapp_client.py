"""WhatsApp Cloud API client (Meta Graph API).

Async send + webhook verification. All credentials come from env via Settings.
"""
import hmac
import hashlib
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.config import Settings


class WhatsAppError(Exception):
    """Raised when the Graph API returns a non-2xx response."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[Any] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class WhatsAppClient:
    """Async client for the WhatsApp Cloud API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _base_url(self) -> str:
        return f"https://graph.facebook.com/{self.settings.whatsapp_api_version}"

    @property
    def _send_url(self) -> str:
        return f"{self._base_url}/{self.settings.whatsapp_phone_number_id}/messages"

    def _auth_headers(self) -> Dict[str, str]:
        return {
            'Authorization': f"Bearer {self.settings.whatsapp_access_token}",
            'Content-Type': 'application/json',
        }

    def _ensure_configured(self) -> None:
        if not self.settings.whatsapp_configured:
            raise WhatsAppError(
                "WhatsApp not configured: set WHATSAPP_ACCESS_TOKEN and "
                "WHATSAPP_PHONE_NUMBER_ID in environment."
            )

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_configured()
        client = await self._http()
        try:
            response = await client.post(self._send_url, headers=self._auth_headers(), json=payload)
        except httpx.HTTPError as e:
            logger.error(f"WhatsApp transport error: {e}")
            raise WhatsAppError(f"Transport error: {e}") from e

        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except Exception:
                body = response.text
            logger.error(f"WhatsApp API error {response.status_code}: {body}")
            raise WhatsAppError(
                f"WhatsApp API returned {response.status_code}",
                status_code=response.status_code,
                response_body=body,
            )

        return response.json()

    async def send_text(self, to: str, body: str, preview_url: bool = False) -> Dict[str, Any]:
        """Send a free-form text message. Recipient must have an open 24h conversation
        window, or use `send_template` for unsolicited / out-of-window sends."""
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type': 'individual',
            'to': to,
            'type': 'text',
            'text': {'preview_url': preview_url, 'body': body},
        }
        return await self._post(payload)

    async def send_template(self, to: str, template_name: str,
                            language_code: str = 'en_US',
                            body_parameters: Optional[List[str]] = None) -> Dict[str, Any]:
        """Send a template message. Use for unsolicited notifications.

        body_parameters is a flat list of strings substituted into the template's
        body component placeholders (`{{1}}`, `{{2}}`, ...).
        """
        components: List[Dict[str, Any]] = []
        if body_parameters:
            components.append({
                'type': 'body',
                'parameters': [{'type': 'text', 'text': p} for p in body_parameters],
            })

        payload: Dict[str, Any] = {
            'messaging_product': 'whatsapp',
            'to': to,
            'type': 'template',
            'template': {
                'name': template_name,
                'language': {'code': language_code},
            },
        }
        if components:
            payload['template']['components'] = components

        return await self._post(payload)

    # ── Webhook helpers ────────────────────────────────────────────────────

    def verify_webhook_challenge(self, mode: Optional[str], token: Optional[str],
                                 challenge: Optional[str]) -> Optional[str]:
        """Validate Meta's GET-verify handshake. Returns the challenge to echo, or None."""
        expected = self.settings.whatsapp_verify_token
        if not expected:
            logger.warning("WHATSAPP_VERIFY_TOKEN not set; refusing webhook verification")
            return None
        if mode == 'subscribe' and token == expected and challenge is not None:
            return challenge
        logger.warning(f"Webhook verification failed: mode={mode} token_match={token == expected}")
        return None

    def verify_signature(self, raw_body: bytes, signature_header: Optional[str]) -> bool:
        """Validate `X-Hub-Signature-256` HMAC against WHATSAPP_APP_SECRET.

        Returns False (not raise) on any mismatch — caller decides response code.
        """
        if not self.settings.whatsapp_app_secret:
            logger.warning("WHATSAPP_APP_SECRET not set; signature verification disabled")
            return False
        if not signature_header or not signature_header.startswith('sha256='):
            return False
        provided = signature_header.split('=', 1)[1]
        expected = hmac.new(
            self.settings.whatsapp_app_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(provided, expected)
