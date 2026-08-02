from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings
from .logging_config import log_event


class NotificationClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _source(self) -> str:
        if self._settings.profile:
            return f"mt5-trader.{self._settings.profile}"
        return "mt5-trader"

    async def notify_signal_outcome(self, summary: dict[str, Any]) -> None:
        if not self._settings.notifications_enabled:
            return
        channels = sorted(self._settings.notification_channels)
        if not channels:
            return

        symbol = str(summary.get("symbol", ""))
        direction = str(summary.get("direction", ""))
        state = str(summary.get("state", ""))
        subject = f"{symbol} {direction} — {state}".strip()

        lines = [
            f"signal_id: {summary.get('signal_id')}",
            f"symbol: {symbol}",
            f"direction: {direction}",
            f"volume: {summary.get('volume')}",
            f"state: {state}",
            f"signal_source: {summary.get('signal_source')}",
            f"profile: {summary.get('profile')}",
        ]
        if summary.get("outcome") is not None:
            lines.append(f"outcome: {summary.get('outcome')}")
        if summary.get("error") is not None:
            lines.append(f"error: {summary.get('error')}")

        payload = {
            "subject": subject,
            "message": "\n".join(lines),
            "contentType": "text",
            "channels": channels,
            "source": self._source,
        }

        headers = {"Content-Type": "application/json"}
        api_key = self._settings.notification_api_key
        if api_key is not None:
            secret = api_key.get_secret_value()
            headers["Authorization"] = f"Bearer {secret}"
            headers["X-API-Key"] = secret

        url = f"{self._settings.notification_service_url.rstrip('/')}/notifications"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=30.0,
                )
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - notification must not affect trading
            log_event(
                "notification_failed",
                level=logging.WARNING,
                signal_id=summary.get("signal_id"),
                error=str(exc),
                exc_info=isinstance(exc, Exception),
            )
