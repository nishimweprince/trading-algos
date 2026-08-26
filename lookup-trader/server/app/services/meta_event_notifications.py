"""Best-effort outbound notifications for newly discovered live meta events.

Delivery is ta_notify.SyncNotifier; what stays here is the part that is actually
lookup-trader's -- the domain payload in ``_payload``, the two source names, and
the startup validation that turns a misconfigured notifier into a SystemExit
rather than a silent no-op.

Sync rather than async on purpose: ``notify_operational`` is called from
ExecutionHeartbeatMonitor on a plain threading.Thread, where there is no event
loop to await into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr
from ta_notify import ALLOWED_CHANNELS, NotificationResult, SyncNotifier

from app.config import Settings
from app.services.meta_events import STOP_ATR, TARGET_ATR, indicative_price_levels

__all__ = ["ALLOWED_CHANNELS", "MetaEventNotifier", "NotificationResult"]

SHADOW_SOURCE = "lookup-trader.meta-shadow"
OPERATIONS_SOURCE = "lookup-trader.operations"


@dataclass(frozen=True)
class _NotifierSettings:
    """Satisfies ta_notify.SupportsNotification.

    lookup-trader's own Settings cannot be used directly and cannot take the
    NotificationSettings mixin: it sets env_prefix="LOOKUP_", and pydantic's
    validation_alias bypasses env_prefix entirely, so mixing in would silently
    open a second, unprefixed environment namespace. Its enable flag is also
    named meta_event_notifications_enabled, and its notification_channels is a
    plain str where the Protocol wants a frozenset property.
    """

    notifications_enabled: bool
    notification_service_url: str
    notification_api_key: SecretStr | None
    notification_timeout_seconds: float
    profile: str | None = None
    channels: frozenset[str] = field(default_factory=frozenset)

    @property
    def notification_channels(self) -> frozenset[str]:
        return self.channels


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
        client: httpx.Client | None = None,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout_seconds = float(timeout_seconds)
        # Production leaves this None, so SyncNotifier builds a short-lived
        # client per call -- the shape the urllib code had. Tests inject one
        # carrying an httpx.MockTransport.
        self._client = client
        self.channels = self._parse_channels(channels) if enabled else ()
        if not enabled:
            self._build_notifiers()
            return
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Notification service URL must be an absolute HTTP(S) base URL")
        if not self.channels:
            raise ValueError("At least one notification channel is required")
        if self.timeout_seconds <= 0:
            raise ValueError("Notification timeout must be greater than zero")
        self._build_notifiers()

    def _build_notifiers(self) -> None:
        config = _NotifierSettings(
            notifications_enabled=self.enabled,
            notification_service_url=self.base_url,
            notification_api_key=SecretStr(self.api_key) if self.api_key else None,
            notification_timeout_seconds=self.timeout_seconds,
            channels=frozenset(self.channels),
        )
        # Two sources, so two notifiers: the receiving end distinguishes
        # discovery traffic from operational alerts.
        self._shadow = SyncNotifier(config, self._client, source=SHADOW_SOURCE)
        self._operations = SyncNotifier(config, self._client, source=OPERATIONS_SOURCE)

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

    def notify(
        self, event: dict[str, Any], predictions: list[dict[str, Any]]
    ) -> NotificationResult:
        subject, lines = self._payload(event, predictions)
        return self._shadow.send(
            subject,
            lines,
            idempotency_key=f"meta-event:{event['event_id']}",
            event_id=event["event_id"],
        )

    def notify_operational(
        self, *, subject: str, message: str, idempotency_key: str
    ) -> NotificationResult:
        return self._operations.send(
            subject,
            message.split("\n"),
            idempotency_key=idempotency_key,
        )

    def _payload(
        self, event: dict[str, Any], predictions: list[dict[str, Any]]
    ) -> tuple[str, list[str]]:
        """The subject and body lines. ta_notify owns the envelope."""
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

        def price(value: Any) -> str:
            return "—" if value is None else f"{float(value):.2f}"

        dropped = [
            str(value).replace("_", " ") for value in empirical.get("dropped_dimensions", [])
        ]
        context = (
            f"Broader · {' + '.join(dropped)} dropped"
            if empirical.get("fallback_used")
            else "Exact context"
        )
        execution_candidate = bool(active and active.get("orders_enabled"))
        lines = [
            (
                "LIVE EXECUTION CANDIDATE — PROVIDER GATES APPLY"
                if execution_candidate
                else "RESEARCH SHADOW — NO ORDER PLACED"
            ),
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
            if active["would_take"]:
                try:
                    levels = indicative_price_levels(
                        float(event["signal_close"]),
                        float(event["atr_at_signal"]),
                        int(event["side"]),
                    )
                except (KeyError, TypeError, ValueError):
                    levels = None
                if levels is not None:
                    lines.extend(
                        [
                            "",
                            "INDICATIVE LEVELS — SIGNAL-CLOSE ANCHOR",
                            f"Reference: {price(levels['reference_price'])}",
                            (f"Stop loss: {price(levels['stop_price'])} ({STOP_ATR:g}× ATR)"),
                            (f"Take profit: {price(levels['target_price'])} ({TARGET_ATR:g}× ATR)"),
                            "Final entry, stop, and target reset from the next H1 open.",
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
        subject = (
            f"{event['symbol']} {event['timeframe']} {side} meta event — "
            f"{event['primary_setup_id']}"
        )
        return subject, lines
