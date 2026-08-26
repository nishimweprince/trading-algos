"""Optional notification-service client. Failures never raise."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings
from .logging_config import log_event

SOURCE = "session-hedging"


class Notifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._url = f"{settings.notification_service_url.rstrip('/')}/notifications"

    @property
    def enabled(self) -> bool:
        return self._s.notifications_enabled and bool(self._s.notification_channels)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._s.notification_api_key
        if api_key is not None:
            secret = api_key.get_secret_value()
            headers["Authorization"] = f"Bearer {secret}"
            headers["X-API-Key"] = secret
        return headers

    async def send(self, subject: str, lines: list[str], **context: Any) -> None:
        if not self.enabled:
            return
        payload = {
            "subject": subject,
            "message": "\n".join(lines),
            "contentType": "text",
            "channels": sorted(self._s.notification_channels),
            "source": SOURCE,
        }
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers=self._headers(),
                timeout=self._s.notification_timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log_event(
                "notification_failed",
                level=logging.WARNING,
                subject=subject,
                error=str(exc),
                **context,
            )
