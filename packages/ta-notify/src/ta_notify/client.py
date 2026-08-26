"""Client for notification-service (``POST /notifications``).

The one hard rule, carried over from all four copies this replaces: **send never
raises.** A notification failure must not propagate into a trading path, so
every exception is logged and swallowed. That is deliberately the opposite of
the execution client's contract, where a silently dropped order submission is
the worst failure mode available.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx
from ta_core import log_event


class SupportsNotification(Protocol):
    """The settings surface this client needs.

    A Protocol rather than a concrete class so a service can satisfy it with its
    own settings object — which is how all four original copies were written,
    each against its own `Settings`.
    """

    notifications_enabled: bool
    notification_service_url: str
    notification_api_key: Any
    notification_timeout_seconds: float
    profile: str | None

    @property
    def notification_channels(self) -> frozenset[str]: ...


class Notifier:
    def __init__(
        self,
        settings: SupportsNotification,
        client: httpx.AsyncClient,
        *,
        source: str,
    ) -> None:
        self._s = settings
        self._client = client
        self._base_source = source
        self._url = f"{settings.notification_service_url.rstrip('/')}/notifications"

    @property
    def enabled(self) -> bool:
        return self._s.notifications_enabled and bool(self._s.notification_channels)

    @property
    def source(self) -> str:
        """Profile-suffixed so `ipda.forex` and `ipda.deriv` stay distinguishable."""
        if self._s.profile:
            return f"{self._base_source}.{self._s.profile}"
        return self._base_source

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._s.notification_api_key
        if api_key is not None:
            secret = api_key.get_secret_value()
            # Both headers: the service has accepted either across its history,
            # and sending both keeps older deployments working.
            headers["Authorization"] = f"Bearer {secret}"
            headers["X-API-Key"] = secret
        return headers

    async def send(self, subject: str, lines: list[str], **context: Any) -> None:
        """Post one text notification. Never raises."""
        if not self.enabled:
            return

        payload = {
            "subject": subject,
            "message": "\n".join(lines),
            "contentType": "text",
            "channels": sorted(self._s.notification_channels),
            "source": self.source,
        }
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers=self._headers(),
                timeout=self._s.notification_timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - notification must not affect trading
            log_event(
                "notification_failed",
                level=logging.WARNING,
                subject=subject,
                error=str(exc),
                **context,
            )
