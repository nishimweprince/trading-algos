"""Best-effort outbound notifications for newly discovered live meta events."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.config import Settings

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS = frozenset({"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"})


@dataclass(frozen=True)
class NotificationResult:
    status: Literal["sent", "remote_skipped", "failed", "disabled"]
    request_id: str | None = None


class MetaEventNotifier:
    """Submit one multi-channel request; never retry or raise delivery failures."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str | None,
        channels: str,
        timeout_seconds: float,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout_seconds = float(timeout_seconds)
        self.channels = self._parse_channels(channels) if enabled else ()
        if not enabled:
            return
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Notification service URL must be an absolute HTTP(S) base URL")
        if not self.channels:
            raise ValueError("At least one notification channel is required")
        if self.timeout_seconds <= 0:
            raise ValueError("Notification timeout must be greater than zero")

    @staticmethod
    def _parse_channels(value: str) -> tuple[str, ...]:
        channels = tuple(
            dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip())
        )
        invalid = sorted(set(channels) - ALLOWED_CHANNELS)
        if invalid:
            raise ValueError(f"Unsupported notification channels: {', '.join(invalid)}")
        return channels

    @classmethod
    def from_settings(cls, config: Settings) -> MetaEventNotifier:
        api_key = (
            config.notification_api_key.get_secret_value()
            if config.notification_api_key is not None
            else None
        )
        return cls(
            enabled=config.meta_event_notifications_enabled,
            base_url=config.notification_service_url,
            api_key=api_key,
            channels=config.notification_channels,
            timeout_seconds=config.notification_timeout_seconds,
        )

    def _send(self, payload: dict[str, Any]) -> NotificationResult:
        if not self.enabled:
            return NotificationResult("disabled")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/notifications",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                status = int(response.status)
                body = json.loads(response.read().decode("utf-8"))
            if status == 201 and isinstance(body, dict) and body.get("requestId"):
                return NotificationResult("sent", str(body["requestId"]))
            if status == 200 and isinstance(body, dict) and body.get("status") == "skipped":
                return NotificationResult("remote_skipped", body.get("requestId"))
            logger.warning(
                "Meta-event notification returned an unexpected response (HTTP %s)", status
            )
        except Exception as exc:  # best effort: never break live discovery
            logger.warning(
                "Meta-event notification failed (%s); event remains persisted",
                type(exc).__name__,
            )
        return NotificationResult("failed")

    def notify(
        self, event: dict[str, Any], predictions: list[dict[str, Any]]
    ) -> NotificationResult:
        payload = self._payload(event, predictions)
        payload["idempotencyKey"] = f"meta-event:{event['event_id']}"
        return self._send(payload)

    def notify_operational(
        self, *, subject: str, message: str, idempotency_key: str
    ) -> NotificationResult:
        return self._send(
            {
                "subject": subject,
                "message": message,
                "contentType": "text",
                "channels": list(self.channels),
                "source": "lookup-trader.operations",
                "idempotencyKey": idempotency_key,
            }
        )

    def _payload(
        self, event: dict[str, Any], predictions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        side = "LONG" if int(event["side"]) == 1 else "SHORT"
        active = next(
            (row for row in predictions if row.get("role") == "active"),
            predictions[0] if predictions else None,
        )
        empirical = event.get("empirical_history") or {}
        recommendation = empirical.get("recommendation") or {}
        headline = str(recommendation.get("headline") or "Unavailable")
        rationale = str(
            recommendation.get("rationale")
            or "The empirical history snapshot could not be calculated at signal time."
        )

        def pct(value: Any) -> str:
            return "—" if value is None else f"{float(value) * 100:.1f}%"

        def r_value(value: Any, *, signed: bool = False) -> str:
            if value is None:
                return "—"
            number = float(value)
            prefix = "+" if signed and number > 0 else ""
            return f"{prefix}{number:.2f}R"

        dropped = [
            str(value).replace("_", " ")
            for value in empirical.get("dropped_dimensions", [])
        ]
        context = (
            f"Broader · {' + '.join(dropped)} dropped"
            if empirical.get("fallback_used")
            else "Exact context"
        )
        lines = [
            "RESEARCH SHADOW — NO ORDER PLACED",
            "",
            f"{event['symbol']} {event['timeframe']} · {side}",
            f"Signal UTC: {event['signal_ts']}",
            f"Setup: {event['primary_setup_id']}",
            f"Confluence: {', '.join(event['setup_ids'])}",
            "",
            "EMPIRICAL HISTORY",
            f"Recommendation: {headline.upper()}",
            f"Reason: {rationale}",
            f"Estimated net: {r_value(empirical.get('expectancy_r_net'), signed=True)}",
            (
                "95% range: "
                f"{r_value(empirical.get('net_expectancy_ci_low_r'), signed=True)} to "
                f"{r_value(empirical.get('net_expectancy_ci_high_r'), signed=True)}"
            ),
            (
                f"Win rate: {pct(empirical.get('win_rate'))} · "
                f"{empirical.get('resolved_count') or 0} resolved bars · "
                f"{empirical.get('independent_periods') or 0} weeks"
            ),
            f"Context: {context}",
        ]
        if active is not None:
            lines.extend(
                [
                    "",
                    "MODEL RECOMMENDATION",
                    f"Recommended direction: {side}",
                    f"Would take: {'YES' if active['would_take'] else 'NO — SKIP'}",
                    f"Positive net outcome probability: {pct(active['probability'])}",
                    f"Take threshold: {pct(active['threshold'])}",
                    f"Active artifact: {active['artifact_version']}",
                ]
            )
        lines.extend(
            [
                "",
                "SIGNAL CONTEXT",
                f"Confidence: {float(event['confidence']):.4f}",
                f"Signal close: {float(event['signal_close']):.5f}",
                f"ATR at signal: {float(event['atr_at_signal']):.5f}",
                (
                    "Calendar coverage: "
                    f"{'trusted' if event['calendar_coverage_ok'] else 'unavailable'}"
                ),
                "Contract: next H1 open | stop 2 ATR | target 3 ATR | maximum 24 observed bars",
                f"Event: {event['event_id']}",
            ]
        )
        return {
            "subject": (
                f"{event['symbol']} {event['timeframe']} {side} meta event — "
                f"{event['primary_setup_id']}"
            ),
            "message": "\n".join(lines),
            "contentType": "text",
            "channels": list(self.channels),
            "source": "lookup-trader.meta-shadow",
        }


__all__ = ["ALLOWED_CHANNELS", "MetaEventNotifier", "NotificationResult"]
