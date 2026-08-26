from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ...config import Settings
from ...logging_config import log_event


def _format_stop_adjustments(adjustments: dict[str, Any]) -> str:
    lines: list[str] = []
    for leg in ("stop_loss", "take_profit"):
        detail = adjustments.get(leg)
        if not isinstance(detail, dict):
            continue
        label = "SL" if leg == "stop_loss" else "TP"
        lines.append(
            f"{label} widened: requested {detail.get('requested_distance')}, "
            f"applied {detail.get('applied_distance')} "
            f"(broker minimum {detail.get('minimum_distance')})"
        )
    return "\n".join(lines)


class NotificationClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _source(self) -> str:
        if self._settings.profile:
            return f"mt5-trader.{self._settings.profile}"
        return "mt5-trader"

    async def notify_signal_outcome(self, summary: dict[str, Any]) -> None:
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
        stop_adjustments = summary.get("stop_adjustments")
        if isinstance(stop_adjustments, dict) and stop_adjustments:
            lines.append("note: broker stop levels adjusted during execution")
            lines.append(_format_stop_adjustments(stop_adjustments))

        await self._deliver(
            subject=subject,
            message="\n".join(lines),
            signal_id=summary.get("signal_id"),
        )

    async def notify_request_failure(
        self,
        *,
        event: str,
        path: str,
        status_code: int,
        client: str | None = None,
        error: Any | None = None,
    ) -> None:
        profile = self._settings.profile or "default"
        subject = f"mt5-trader {profile} — {event.replace('_', ' ')}"
        lines = [
            f"event: {event}",
            f"path: {path}",
            f"status_code: {status_code}",
            f"profile: {self._settings.profile}",
        ]
        if client:
            lines.append(f"client: {client}")
        if error is not None:
            lines.append(f"error: {json.dumps(error, default=str)}")
        await self._deliver(subject=subject, message="\n".join(lines))

    async def _deliver(
        self,
        *,
        subject: str,
        message: str,
        signal_id: Any | None = None,
    ) -> None:
        if not self._settings.notifications_enabled:
            return
        channels = sorted(self._settings.notification_channels)
        if not channels:
            return

        payload = {
            "subject": subject,
            "message": message,
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
                signal_id=signal_id,
                error=str(exc),
                exc_info=isinstance(exc, Exception),
            )
