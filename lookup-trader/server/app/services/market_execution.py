"""Fail-closed HTTP market execution and provider heartbeat supervision."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app.config import Settings
from app.services.meta_event_notifications import MetaEventNotifier
from app.services.meta_events import STOP_ATR, TARGET_ATR
from app.services.meta_shadow_store import MetaShadowStore

logger = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CTRADER_ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ORDER_PATHS = {"mt5": "/v1/signals", "ctrader": "/v1/orders"}
_HEALTH_PATHS = {"mt5": "/health/ready", "ctrader": "/health/trading-ready"}


def _decimal(value: Decimal) -> str:
    """Stable, non-exponent JSON decimal text for broker contracts."""
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Market order timestamp must include a timezone offset")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_reason(value: object, *, fallback: str) -> str:
    """Return a bounded reason that cannot accidentally include response bodies."""
    text = str(value).strip().replace("\n", " ")
    return (text[:240] if text else fallback).replace("Authorization", "authentication")


def _redact_known_secret(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, dict):
        return {key: _redact_known_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_known_secret(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    return value


@dataclass(frozen=True)
class ExecutionConfig:
    enabled: bool
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    volume_lots: Decimal | None = None
    ctrader_account: str | None = None
    source: str = "lookup_trader"
    symbol_map: dict[str, str] | None = None
    timeout_seconds: float = 5.0
    heartbeat_interval_seconds: float = 300.0
    max_event_age_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings: Settings) -> ExecutionConfig:
        if not settings.market_execution_enabled:
            return cls(enabled=False)

        explicit = settings.execution_provider
        common_url = (settings.execution_url or "").strip() or None
        specific = {
            "mt5": (settings.mt5_trader_url or "").strip() or None,
            "ctrader": (settings.ctrader_markets_url or "").strip() or None,
        }
        selected_specific = [name for name, value in specific.items() if value]
        if len(selected_specific) > 1:
            raise ValueError("Configure only one provider-specific execution URL")
        if common_url and selected_specific:
            raise ValueError("EXECUTION_URL conflicts with a provider-specific execution URL")

        provider = explicit
        raw_url = common_url
        if selected_specific:
            inferred = selected_specific[0]
            if provider is not None and provider != inferred:
                raise ValueError("EXECUTION_PROVIDER conflicts with the provider-specific URL")
            provider, raw_url = inferred, specific[inferred]
        elif raw_url and provider is None:
            path = urlsplit(raw_url).path.rstrip("/")
            matches = [name for name, order_path in _ORDER_PATHS.items() if path == order_path]
            if len(matches) != 1:
                raise ValueError(
                    "A bare EXECUTION_URL is ambiguous; set EXECUTION_PROVIDER or use a full "
                    "provider order endpoint"
                )
            provider = matches[0]

        if provider is None or raw_url is None:
            raise ValueError("Market execution requires a provider and execution URL")
        base_url = _validated_base_url(raw_url, provider)
        api_key = (
            settings.execution_api_key.get_secret_value()
            if settings.execution_api_key is not None
            else ""
        )
        if not api_key:
            raise ValueError("Market execution requires EXECUTION_API_KEY")
        if settings.execution_volume_lots is None:
            raise ValueError("Market execution requires positive EXECUTION_VOLUME_LOTS")
        account = (settings.execution_ctrader_account or "").strip() or None
        if provider == "ctrader" and (
            account is None or _CTRADER_ACCOUNT_RE.fullmatch(account) is None
        ):
            raise ValueError("cTrader execution requires a valid EXECUTION_CTRADER_ACCOUNT")
        source = settings.execution_source.strip().lower()
        if _SOURCE_RE.fullmatch(source) is None:
            raise ValueError("EXECUTION_SOURCE must be a lowercase source slug")
        symbols = {
            str(symbol).strip().upper(): str(provider_symbol).strip()
            for symbol, provider_symbol in settings.execution_symbol_map.items()
        }
        if any(not symbol or not provider_symbol for symbol, provider_symbol in symbols.items()):
            raise ValueError("EXECUTION_SYMBOL_MAP cannot contain blank symbols")
        return cls(
            enabled=True,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            volume_lots=settings.execution_volume_lots,
            ctrader_account=account,
            source=source,
            symbol_map=symbols,
            timeout_seconds=float(settings.execution_timeout_seconds),
            heartbeat_interval_seconds=float(settings.execution_heartbeat_interval_seconds),
            max_event_age_seconds=float(settings.execution_max_event_age_seconds),
        )

    @property
    def account_key(self) -> str:
        return self.ctrader_account or "single-account"


def _validated_base_url(raw_url: str, provider: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Execution URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Execution URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Execution URL must not contain a query string or fragment")
    path = parsed.path.rstrip("/")
    known_provider = next(
        (name for name, endpoint in _ORDER_PATHS.items() if path == endpoint), None
    )
    if known_provider is not None and known_provider != provider:
        raise ValueError("Execution URL endpoint conflicts with EXECUTION_PROVIDER")
    if known_provider is not None:
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


@dataclass(frozen=True)
class MarketOrder:
    request_id: str
    occurred_at: datetime
    symbol: str
    direction: str
    volume_lots: Decimal
    stop_loss_distance: Decimal
    take_profit_distance: Decimal
    source: str
    ctrader_account: str | None = None


class ExecutionState(StrEnum):
    RESERVED = "reserved"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ProviderResult:
    state: ExecutionState
    response: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    healthy: bool
    reason: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> HttpResponse:
        request_headers = dict(headers or {})
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return HttpResponse(int(response.status), response.read())
        except HTTPError as exc:
            # HTTP errors are contract outcomes. The bounded body is parsed by
            # the adapter but is never included in exception/log text.
            return HttpResponse(int(exc.code), exc.read(64 * 1024))


class ExecutionProvider(Protocol):
    name: str

    def submit(self, order: MarketOrder) -> ProviderResult: ...

    def lookup(self, request_id: str) -> ProviderResult: ...

    def heartbeat(self) -> HeartbeatResult: ...


class HttpExecutionProvider:
    name: str

    def __init__(self, config: ExecutionConfig, transport: Transport | None = None) -> None:
        if not config.enabled or config.base_url is None or config.api_key is None:
            raise ValueError("Cannot construct an execution provider from disabled settings")
        self.config = config
        self.base_url = config.base_url
        self.transport = transport or UrllibTransport()

    @property
    def _auth(self) -> dict[str, str]:
        return {"X-API-Key": self.config.api_key or ""}

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, Any] | None]:
        response = self.transport.request(
            method,
            f"{self.base_url}{path}",
            headers=self._auth if authenticated else None,
            payload=payload,
            timeout=self.config.timeout_seconds,
        )
        try:
            body = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        return response.status, body if isinstance(body, dict) else None

    def heartbeat(self) -> HeartbeatResult:
        try:
            status, body = self._call("GET", _HEALTH_PATHS[self.name], authenticated=False)
        except Exception as exc:  # transport failures are expected outage evidence
            return HeartbeatResult(False, type(exc).__name__)
        if status != 200:
            return HeartbeatResult(False, f"HTTP {status}")
        if body is None:
            return HeartbeatResult(False, "malformed JSON response")
        if body.get("status") != "ready":
            return HeartbeatResult(False, "provider status is not ready")
        return HeartbeatResult(True)


class MT5ExecutionProvider(HttpExecutionProvider):
    name = "mt5"

    def submit(self, order: MarketOrder) -> ProviderResult:
        payload = {
            "signal_id": order.request_id,
            "occurred_at": _timestamp(order.occurred_at),
            "execution_type": "market",
            "symbol": order.symbol,
            "direction": order.direction,
            "volume": _decimal(order.volume_lots),
            "stop_loss_distance": _decimal(order.stop_loss_distance),
            "take_profit_distance": _decimal(order.take_profit_distance),
            "source": order.source,
        }
        return self._submit_or_lookup("POST", "/v1/signals", payload)

    def lookup(self, request_id: str) -> ProviderResult:
        return self._submit_or_lookup("GET", f"/v1/signals/{request_id}")

    def _submit_or_lookup(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> ProviderResult:
        try:
            status, body = self._call(method, path, payload=payload)
        except Exception as exc:
            return ProviderResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        if status == 404 and method == "GET":
            return ProviderResult(ExecutionState.NOT_FOUND)
        if body is None:
            return ProviderResult(
                ExecutionState.UNKNOWN, reason=f"malformed response (HTTP {status})"
            )
        if not 200 <= status < 300:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            return ProviderResult(
                ExecutionState.REJECTED if status in {409, 422} else ExecutionState.UNKNOWN,
                response=body,
                reason=_safe_reason(error.get("code"), fallback=f"HTTP {status}"),
            )
        outcome = body.get("outcome")
        if outcome in {"filled", "partially_filled", "placed"}:
            return ProviderResult(ExecutionState.SUCCEEDED, response=body)
        state = body.get("state")
        if state in {"received", "executing"}:
            return ProviderResult(ExecutionState.PENDING, response=body)
        if state in {"filled", "partially_filled", "placed"}:
            return ProviderResult(ExecutionState.SUCCEEDED, response=body)
        if state in {"rejected"}:
            return ProviderResult(ExecutionState.REJECTED, response=body, reason="rejected")
        return ProviderResult(ExecutionState.UNKNOWN, response=body, reason="ambiguous response")


class CTraderExecutionProvider(HttpExecutionProvider):
    name = "ctrader"

    def submit(self, order: MarketOrder) -> ProviderResult:
        if order.ctrader_account is None:
            return ProviderResult(ExecutionState.REJECTED, reason="missing cTrader account")
        payload = {
            "operation_id": order.request_id,
            "occurred_at": _timestamp(order.occurred_at),
            "source": order.source,
            "instrument": order.symbol,
            "execution_type": "market",
            "direction": order.direction,
            "targets": [
                {
                    "account": order.ctrader_account,
                    "volume_lots": _decimal(order.volume_lots),
                }
            ],
            "stop_loss_distance": _decimal(order.stop_loss_distance),
            "take_profit_distance": _decimal(order.take_profit_distance),
        }
        return self._submit_or_lookup("POST", "/v1/orders", payload)

    def lookup(self, request_id: str) -> ProviderResult:
        return self._submit_or_lookup("GET", f"/v1/operations/{request_id}")

    def _submit_or_lookup(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> ProviderResult:
        try:
            status, body = self._call(method, path, payload=payload)
        except Exception as exc:
            return ProviderResult(ExecutionState.UNKNOWN, reason=type(exc).__name__)
        if status == 404 and method == "GET":
            return ProviderResult(ExecutionState.NOT_FOUND)
        if body is None:
            return ProviderResult(
                ExecutionState.UNKNOWN, reason=f"malformed response (HTTP {status})"
            )
        if not 200 <= status < 300:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            return ProviderResult(
                ExecutionState.REJECTED if status in {409, 422} else ExecutionState.UNKNOWN,
                response=body,
                reason=_safe_reason(error.get("code"), fallback=f"HTTP {status}"),
            )
        state = body.get("state")
        if status == 202 or state in {"pending", "unknown"}:
            return ProviderResult(ExecutionState.PENDING, response=body)
        if state == "succeeded":
            return ProviderResult(ExecutionState.SUCCEEDED, response=body)
        if state in {"partial_failure", "rejected"}:
            return ProviderResult(ExecutionState.REJECTED, response=body, reason=str(state))
        return ProviderResult(ExecutionState.UNKNOWN, response=body, reason="ambiguous response")


def build_provider(
    config: ExecutionConfig, transport: Transport | None = None
) -> ExecutionProvider:
    if config.provider == "mt5":
        return MT5ExecutionProvider(config, transport)
    if config.provider == "ctrader":
        return CTraderExecutionProvider(config, transport)
    raise ValueError("Unsupported execution provider")


class ExecutionHeartbeatMonitor:
    """One persistent, edge-triggered readiness monitor per worker process."""

    def __init__(
        self,
        *,
        provider: ExecutionProvider,
        store: MetaShadowStore,
        notifier: MetaEventNotifier,
        interval_seconds: float,
        clock: Callable[[], datetime] | None = None,
        waiter: Callable[[float], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._waiter = waiter or self._stop.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Execution heartbeat monitor is already started")
        # The synchronous startup probe closes the race in which the first event
        # could otherwise arrive before the background thread ran.
        self.check_once()
        self._thread = threading.Thread(
            target=self._run,
            name=f"{self.provider.name}-execution-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=max(1.0, min(self.interval_seconds, 10.0)))
        self._thread = None

    def _run(self) -> None:
        while not self._waiter(self.interval_seconds):
            self.check_once()

    def check_once(self) -> dict[str, Any]:
        result = self.provider.heartbeat()
        status = self.store.record_execution_heartbeat(
            provider=self.provider.name,
            healthy=result.healthy,
            reason=result.reason,
            checked_at=self.clock(),
        )
        try:
            self._notify_transition(status)
        except Exception as exc:  # notification must never stop readiness monitoring
            logger.warning("Execution heartbeat notification failed (%s)", type(exc).__name__)
        return status

    def _notify_transition(self, status: dict[str, Any]) -> None:
        outage_id = int(status["outage_id"])
        if status["status"] == "unhealthy" and not status["failure_notified"]:
            result = self.notifier.notify_operational(
                subject=f"{self.provider.name} execution provider unavailable",
                message=(
                    f"lookup-trader market execution is gated because the {self.provider.name} "
                    f"provider heartbeat failed: {status['reason'] or 'unknown reason'}."
                ),
                idempotency_key=f"execution-heartbeat:{self.provider.name}:outage:{outage_id}",
            )
            self.store.record_execution_heartbeat_notification(
                provider=self.provider.name,
                kind="failure",
                delivered=result.status in {"sent", "remote_skipped"},
            )
        elif status["recovery_pending"] and not status["recovery_notified"]:
            result = self.notifier.notify_operational(
                subject=f"{self.provider.name} execution provider recovered",
                message=(
                    f"lookup-trader confirmed that the {self.provider.name} execution provider "
                    "is trading-ready again. Normal execution gates still apply."
                ),
                idempotency_key=f"execution-heartbeat:{self.provider.name}:recovery:{outage_id}",
            )
            self.store.record_execution_heartbeat_notification(
                provider=self.provider.name,
                kind="recovery",
                delivered=result.status in {"sent", "remote_skipped"},
            )


class MarketExecutionCoordinator:
    """Apply local gates, reserve one attempt, then use remote idempotency."""

    def __init__(
        self,
        *,
        config: ExecutionConfig,
        provider: ExecutionProvider,
        store: MetaShadowStore,
        notifier: MetaEventNotifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monitor = ExecutionHeartbeatMonitor(
            provider=provider,
            store=store,
            notifier=notifier,
            interval_seconds=config.heartbeat_interval_seconds,
            clock=self.clock,
        )

    def start(self) -> None:
        self.monitor.start()

    def stop(self) -> None:
        self.monitor.stop()

    def execute_if_eligible(
        self,
        *,
        event: dict[str, Any],
        active_prediction: dict[str, Any] | None,
        pointer_orders_enabled: bool,
    ) -> dict[str, Any] | None:
        if not self.config.enabled or active_prediction is None:
            return None
        if active_prediction.get("role") != "active" or not active_prediction.get("would_take"):
            return None
        if not pointer_orders_enabled or active_prediction.get("orders_enabled") is not True:
            return None
        if not event.get("forward_evaluation_eligible") or event.get("ineligible_reason"):
            return None
        if not event.get("calendar_coverage_ok"):
            return None
        if event.get("side") not in {-1, 1} or not str(event.get("symbol") or "").strip():
            return None
        try:
            signal_ts = _aware_datetime(event.get("signal_ts"))
        except (TypeError, ValueError):
            return None
        age = (self.clock() - signal_ts).total_seconds()
        if age < 0 or age > self.config.max_event_age_seconds:
            return None
        health = self.store.execution_heartbeat_status(self.provider.name)
        if health is None or health.get("status") != "healthy":
            return None
        try:
            atr = Decimal(str(event.get("atr_at_signal")))
        except (InvalidOperation, ValueError):
            return None
        if not atr.is_finite() or atr <= 0:
            return None
        request_id = str(event["event_id"])
        attempt, created = self.store.reserve_execution(
            event_id=request_id,
            provider=self.provider.name,
            account_key=self.config.account_key,
            request_id=request_id,
        )
        if attempt["state"] in {ExecutionState.SUCCEEDED, ExecutionState.REJECTED}:
            return attempt
        return self._continue_attempt(event, attempt, newly_reserved=created)

    def reconcile_outstanding(self) -> list[dict[str, Any]]:
        """Resolve crash/pending ambiguity without creating a new logical order."""
        reconciled = []
        for event, attempt in self.store.outstanding_executions(
            provider=self.provider.name, account_key=self.config.account_key
        ):
            updated = self._continue_attempt(event, attempt, newly_reserved=False)
            if updated is not None:
                reconciled.append(updated)
        return reconciled

    def _continue_attempt(
        self,
        event: dict[str, Any],
        attempt: dict[str, Any],
        *,
        newly_reserved: bool,
    ) -> dict[str, Any] | None:
        request_id = str(attempt["request_id"])
        signal_ts = _aware_datetime(event.get("signal_ts"))
        # A crash can happen after local reservation or after a remote request.
        # Query first on every resumed attempt, and only submit if the provider
        # proves the deterministic id does not exist.
        result = (
            ProviderResult(ExecutionState.NOT_FOUND)
            if newly_reserved
            else self.provider.lookup(request_id)
        )
        if result.state is ExecutionState.NOT_FOUND:
            age = (self.clock() - signal_ts).total_seconds()
            if age < 0 or age > self.config.max_event_age_seconds:
                result = ProviderResult(
                    ExecutionState.REJECTED,
                    reason="event expired before remote submission",
                )
            else:
                result = self.provider.submit(self._market_order(event, signal_ts))
        self.store.update_execution(
            event_id=str(event["event_id"]),
            provider=self.provider.name,
            account_key=self.config.account_key,
            state=result.state.value,
            response=_redact_known_secret(result.response, self.config.api_key),
            error_reason=_redact_known_secret(result.reason, self.config.api_key),
        )
        return self.store.execution_attempt(
            event_id=str(event["event_id"]),
            provider=self.provider.name,
            account_key=self.config.account_key,
        )

    def _market_order(self, event: dict[str, Any], signal_ts: datetime) -> MarketOrder:
        atr = Decimal(str(event.get("atr_at_signal")))
        return MarketOrder(
            request_id=str(event["event_id"]),
            occurred_at=signal_ts,
            symbol=(self.config.symbol_map or {}).get(
                str(event["symbol"]).upper(), str(event["symbol"]).upper()
            ),
            direction="buy" if int(event["side"]) == 1 else "sell",
            volume_lots=self.config.volume_lots or Decimal("0"),
            stop_loss_distance=atr * Decimal(str(STOP_ATR)),
            take_profit_distance=atr * Decimal(str(TARGET_ATR)),
            source=self.config.source,
            ctrader_account=self.config.ctrader_account,
        )


def _aware_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Event timestamp must include a timezone offset")
    return stamp.astimezone(UTC)


def execution_status(settings: Settings, store: MetaShadowStore) -> dict[str, Any]:
    """Secret-safe status projection for API health surfaces."""
    try:
        config = ExecutionConfig.from_settings(settings)
    except ValueError as exc:
        return {
            "enabled": bool(settings.market_execution_enabled),
            "provider": settings.execution_provider,
            "status": "invalid_configuration",
            "last_heartbeat": None,
            "last_success": None,
            "consecutive_failures": 0,
            "reason": _safe_reason(exc, fallback="invalid configuration"),
        }
    if not config.enabled or config.provider is None:
        return {
            "enabled": False,
            "provider": None,
            "status": "disabled",
            "last_heartbeat": None,
            "last_success": None,
            "consecutive_failures": 0,
            "reason": None,
        }
    heartbeat = store.execution_heartbeat_status(config.provider) or {}
    return {
        "enabled": True,
        "provider": config.provider,
        "status": heartbeat.get("status", "unknown"),
        "last_heartbeat": heartbeat.get("last_checked_at"),
        "last_success": heartbeat.get("last_success_at"),
        "consecutive_failures": int(heartbeat.get("consecutive_failures", 0)),
        "reason": heartbeat.get("reason"),
    }
