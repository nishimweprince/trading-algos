from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.market_execution import (
    CTraderExecutionProvider,
    ExecutionConfig,
    ExecutionHeartbeatMonitor,
    ExecutionState,
    HeartbeatResult,
    HttpResponse,
    MarketExecutionCoordinator,
    MarketOrder,
    MT5ExecutionProvider,
    ProviderResult,
)
from app.services.meta_event_notifications import NotificationResult
from app.services.meta_shadow_store import MetaShadowStore


class FakeTransport:
    def __init__(self, responses=None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, method, url, *, headers, payload, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _http(status: int, body: object) -> HttpResponse:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HttpResponse(status, raw)


def _config(provider="mt5", **overrides) -> ExecutionConfig:
    values = {
        "enabled": True,
        "provider": provider,
        "base_url": "http://broker.internal:8000",
        "api_key": "private-key",
        "volume_lots": Decimal("0.10"),
        "ctrader_account": "demo" if provider == "ctrader" else None,
        "source": "lookup_trader",
        "symbol_map": {},
        "timeout_seconds": 4.0,
        "heartbeat_interval_seconds": 300.0,
        "max_event_age_seconds": 300.0,
    }
    values.update(overrides)
    return ExecutionConfig(**values)


def _settings(**overrides) -> Settings:
    values = {
        "market_execution_enabled": True,
        "execution_provider": "mt5",
        "execution_url": "http://broker.internal:8000",
        "execution_api_key": SecretStr("private-key"),
        "execution_volume_lots": Decimal("0.1"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _order(**overrides) -> MarketOrder:
    values = {
        "request_id": "b38c40a0-3731-4d50-a286-e8cefa5707cc",
        "occurred_at": datetime(2026, 8, 23, 10, tzinfo=UTC),
        "symbol": "XAUUSD",
        "direction": "buy",
        "volume_lots": Decimal("0.10"),
        "stop_loss_distance": Decimal("40.0"),
        "take_profit_distance": Decimal("60.0"),
        "source": "lookup_trader",
        "ctrader_account": None,
    }
    values.update(overrides)
    return MarketOrder(**values)


def _event(now: datetime, **overrides):
    values = {
        "event_id": str(uuid4()),
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "signal_ts": now - timedelta(seconds=60),
        "side": 1,
        "primary_setup_id": "bull_engulfing",
        "setup_ids": ["bull_engulfing"],
        "confidence": 0.9,
        "state": "awaiting_entry",
        "ineligible_reason": None,
        "forward_evaluation_eligible": True,
        "calendar_coverage_ok": True,
        "calendar_manifest_sha256": "a" * 64,
        "causal_features_v1": {},
        "causal_features_v2": {},
        "signal_close": 3500.0,
        "atr_at_signal": 20.0,
        "source_boundary": now - timedelta(days=1),
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "execution_provider": None,
                "execution_url": "http://broker.internal:8000",
            },
            "ambiguous",
        ),
        (
            {
                "execution_url": None,
                "mt5_trader_url": "http://mt5:8000",
                "ctrader_markets_url": "http://ctrader:8010",
            },
            "only one provider-specific",
        ),
        (
            {
                "execution_provider": "mt5",
                "execution_url": "http://broker:8000",
                "mt5_trader_url": "http://mt5:8000",
            },
            "conflicts",
        ),
        ({"execution_url": "http://user:pass@broker:8000"}, "embedded credentials"),
        ({"execution_api_key": None}, "API_KEY"),
        ({"execution_volume_lots": None}, "VOLUME_LOTS"),
        (
            {
                "execution_provider": "ctrader",
                "execution_ctrader_account": None,
            },
            "CTRADER_ACCOUNT",
        ),
    ],
)
def test_enabled_execution_configuration_fails_closed(overrides, message):
    with pytest.raises(ValueError, match=message):
        ExecutionConfig.from_settings(_settings(**overrides))


def test_provider_can_be_selected_by_full_endpoint_or_specific_url():
    inferred = ExecutionConfig.from_settings(
        _settings(
            execution_provider=None,
            execution_url="https://broker.example/v1/orders",
            execution_ctrader_account="demo-a",
        )
    )
    assert inferred.provider == "ctrader"
    assert inferred.base_url == "https://broker.example"

    specific = ExecutionConfig.from_settings(
        _settings(
            execution_provider=None,
            execution_url=None,
            mt5_trader_url="https://mt5.example/v1/signals",
        )
    )
    assert specific.provider == "mt5"
    assert specific.base_url == "https://mt5.example"


def test_disabled_execution_ignores_partial_provider_configuration():
    settings = Settings(
        _env_file=None,
        market_execution_enabled=False,
        execution_url="not-a-url",
    )
    assert ExecutionConfig.from_settings(settings) == ExecutionConfig(enabled=False)


def test_mt5_market_payload_matches_existing_contract():
    transport = FakeTransport([_http(200, {"signal_id": _order().request_id, "outcome": "filled"})])
    provider = MT5ExecutionProvider(_config(), transport)

    result = provider.submit(_order())

    assert result.state is ExecutionState.SUCCEEDED
    assert transport.calls == [
        {
            "method": "POST",
            "url": "http://broker.internal:8000/v1/signals",
            "headers": {"X-API-Key": "private-key"},
            "payload": {
                "signal_id": "b38c40a0-3731-4d50-a286-e8cefa5707cc",
                "occurred_at": "2026-08-23T10:00:00Z",
                "execution_type": "market",
                "symbol": "XAUUSD",
                "direction": "buy",
                "volume": "0.1",
                "stop_loss_distance": "40",
                "take_profit_distance": "60",
                "source": "lookup_trader",
            },
            "timeout": 4.0,
        }
    ]
    assert "entry_price" not in transport.calls[0]["payload"]
    assert "expires_at" not in transport.calls[0]["payload"]


def test_mt5_durable_status_recovers_a_completed_request():
    request_id = _order().request_id
    transport = FakeTransport(
        [
            _http(
                200,
                {
                    "signal_id": request_id,
                    "state": "filled",
                    "response": {"signal_id": request_id, "outcome": "filled"},
                },
            )
        ]
    )

    result = MT5ExecutionProvider(_config(), transport).lookup(request_id)

    assert result.state is ExecutionState.SUCCEEDED
    assert transport.calls[0]["url"].endswith(f"/v1/signals/{request_id}")


def test_ctrader_market_payload_matches_existing_contract():
    transport = FakeTransport(
        [_http(201, {"operation_id": _order().request_id, "state": "succeeded"})]
    )
    provider = CTraderExecutionProvider(_config("ctrader"), transport)

    result = provider.submit(_order(ctrader_account="demo", direction="sell"))

    assert result.state is ExecutionState.SUCCEEDED
    payload = transport.calls[0]["payload"]
    assert transport.calls[0]["url"] == "http://broker.internal:8000/v1/orders"
    assert payload == {
        "operation_id": "b38c40a0-3731-4d50-a286-e8cefa5707cc",
        "occurred_at": "2026-08-23T10:00:00Z",
        "source": "lookup_trader",
        "instrument": "XAUUSD",
        "execution_type": "market",
        "direction": "sell",
        "targets": [{"account": "demo", "volume_lots": "0.1"}],
        "stop_loss_distance": "40",
        "take_profit_distance": "60",
    }
    assert "entry_price" not in payload
    assert "expires_at" not in payload


@pytest.mark.parametrize(
    ("provider_name", "health_path"),
    [("mt5", "/health/ready"), ("ctrader", "/health/trading-ready")],
)
def test_provider_heartbeat_uses_unauthenticated_trading_readiness(provider_name, health_path):
    transport = FakeTransport([_http(200, {"status": "ready", "details": {}})])
    config = _config(provider_name)
    provider = (
        MT5ExecutionProvider(config, transport)
        if provider_name == "mt5"
        else CTraderExecutionProvider(config, transport)
    )

    assert provider.heartbeat() == HeartbeatResult(True)
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(health_path)
    assert transport.calls[0]["headers"] is None


@pytest.mark.parametrize(
    "transport",
    [
        FakeTransport(error=TimeoutError("sensitive transport detail")),
        FakeTransport([_http(503, {"status": "not_ready"})]),
        FakeTransport([_http(200, b"not-json")]),
        FakeTransport([_http(200, {"status": "not_ready"})]),
    ],
)
def test_heartbeat_transport_http_and_contract_failures_are_unhealthy(transport):
    result = MT5ExecutionProvider(_config(), transport).heartbeat()
    assert result.healthy is False
    assert "sensitive transport detail" not in (result.reason or "")


@pytest.mark.parametrize(
    ("transport", "expected_reason"),
    [
        (FakeTransport(error=TimeoutError("secret detail")), "TimeoutError"),
        (FakeTransport([_http(200, b"not-json")]), "malformed response"),
        (FakeTransport([_http(503, {"error": {"code": "not_ready"}})]), "not_ready"),
    ],
)
def test_provider_failures_are_fail_closed_and_secret_safe(transport, expected_reason):
    result = MT5ExecutionProvider(_config(), transport).submit(_order())
    assert result.state in {ExecutionState.UNKNOWN, ExecutionState.REJECTED}
    assert expected_reason in (result.reason or "")
    assert "secret detail" not in (result.reason or "")


class StubProvider:
    name = "mt5"

    def __init__(self, heartbeats=None):
        self.heartbeats = list(heartbeats or [HeartbeatResult(True)])
        self.heartbeat_calls = 0
        self.submits = []
        self.lookups = []
        self.lookup_results = []

    def heartbeat(self):
        self.heartbeat_calls += 1
        if len(self.heartbeats) > 1:
            return self.heartbeats.pop(0)
        return self.heartbeats[0]

    def submit(self, order):
        self.submits.append(order)
        return ProviderResult(ExecutionState.SUCCEEDED, {"outcome": "filled"})

    def lookup(self, request_id):
        self.lookups.append(request_id)
        if self.lookup_results:
            return self.lookup_results.pop(0)
        return ProviderResult(ExecutionState.NOT_FOUND)


class SpyNotifier:
    enabled = True

    def __init__(self):
        self.calls = []

    def notify_operational(self, **kwargs):
        self.calls.append(kwargs)
        return NotificationResult("sent", f"request-{len(self.calls)}")


def test_heartbeat_outages_are_deduplicated_and_recovery_is_notified(tmp_path):
    clock_values = iter(datetime(2026, 8, 23, 10, minute, tzinfo=UTC) for minute in range(5))
    provider = StubProvider(
        [
            HeartbeatResult(False, "HTTP 503"),
            HeartbeatResult(False, "HTTP 503"),
            HeartbeatResult(True),
            HeartbeatResult(False, "TimeoutError"),
        ]
    )
    notifier = SpyNotifier()
    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    monitor = ExecutionHeartbeatMonitor(
        provider=provider,
        store=store,
        notifier=notifier,
        interval_seconds=300,
        clock=lambda: next(clock_values),
    )

    first = monitor.check_once()
    second = monitor.check_once()
    recovered = monitor.check_once()
    new_outage = monitor.check_once()

    assert first["consecutive_failures"] == 1
    assert second["consecutive_failures"] == 2
    assert recovered["status"] == "healthy"
    assert new_outage["outage_id"] == 2
    assert [call["idempotency_key"] for call in notifier.calls] == [
        "execution-heartbeat:mt5:outage:1",
        "execution-heartbeat:mt5:recovery:1",
        "execution-heartbeat:mt5:outage:2",
    ]


def test_monitor_runs_immediately_then_on_configured_interval_with_fake_clock(tmp_path):
    provider = StubProvider([HeartbeatResult(True), HeartbeatResult(True)])
    notifier = SpyNotifier()
    waits = []
    waiter_results = iter([False, True])
    finished = threading.Event()

    def waiter(seconds):
        waits.append(seconds)
        result = next(waiter_results)
        if result:
            finished.set()
        return result

    monitor = ExecutionHeartbeatMonitor(
        provider=provider,
        store=MetaShadowStore(tmp_path / "meta.sqlite3"),
        notifier=notifier,
        interval_seconds=300,
        clock=lambda: datetime(2026, 8, 23, 10, tzinfo=UTC),
        waiter=waiter,
    )
    monitor.start()
    assert finished.wait(1)
    assert waits == [300, 300]
    assert provider.heartbeat_calls == 2
    with pytest.raises(RuntimeError, match="already started"):
        monitor.start()
    monitor.stop()


def test_heartbeat_notification_failure_does_not_stop_monitoring(tmp_path):
    class RaisingNotifier:
        def notify_operational(self, **kwargs):
            raise RuntimeError("notification unavailable")

    monitor = ExecutionHeartbeatMonitor(
        provider=StubProvider([HeartbeatResult(False, "HTTP 503")]),
        store=MetaShadowStore(tmp_path / "meta.sqlite3"),
        notifier=RaisingNotifier(),
        interval_seconds=300,
        clock=lambda: datetime(2026, 8, 23, 10, tzinfo=UTC),
    )

    result = monitor.check_once()

    assert result["status"] == "unhealthy"
    assert result["consecutive_failures"] == 1


def _coordinator(tmp_path, now, provider=None, config=None):
    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    provider = provider or StubProvider()
    coordinator = MarketExecutionCoordinator(
        config=config or _config(),
        provider=provider,
        store=store,
        notifier=SpyNotifier(),
        clock=lambda: now,
    )
    return coordinator, store, provider


def _healthy(store, now):
    store.record_execution_heartbeat(provider="mt5", healthy=True, reason=None, checked_at=now)


def _active_prediction(**overrides):
    values = {
        "artifact_version": "active-v1",
        "role": "active",
        "would_take": True,
        "orders_enabled": True,
    }
    values.update(overrides)
    return values


def test_execution_is_reserved_idempotent_and_uses_atr_distances(tmp_path):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    coordinator, store, provider = _coordinator(tmp_path, now)
    event = _event(now, atr_at_signal=20.0)
    assert store.insert_event(event)
    _healthy(store, now)

    first = coordinator.execute_if_eligible(
        event=event,
        active_prediction=_active_prediction(),
        pointer_orders_enabled=True,
    )
    second = coordinator.execute_if_eligible(
        event=event,
        active_prediction=_active_prediction(),
        pointer_orders_enabled=True,
    )

    assert first["state"] == "succeeded"
    assert second["state"] == "succeeded"
    assert len(provider.submits) == 1
    assert provider.submits[0].request_id == event["event_id"]
    assert provider.submits[0].stop_loss_distance == Decimal("40")
    assert provider.submits[0].take_profit_distance == Decimal("60")
    assert provider.lookups == []


@pytest.mark.parametrize(
    ("event_overrides", "prediction_overrides", "pointer_enabled", "healthy"),
    [
        ({}, {"role": "challenger"}, True, True),
        ({}, {"would_take": False}, True, True),
        ({}, {"orders_enabled": False}, True, True),
        ({}, {}, False, True),
        ({"forward_evaluation_eligible": False}, {}, True, True),
        ({"ineligible_reason": "data_quality_unreliable"}, {}, True, True),
        ({"calendar_coverage_ok": False}, {}, True, True),
        ({"signal_ts": datetime(2026, 8, 23, 9, tzinfo=UTC)}, {}, True, True),
        ({}, {}, True, False),
    ],
)
def test_execution_safety_gates_never_contact_provider(
    tmp_path,
    event_overrides,
    prediction_overrides,
    pointer_enabled,
    healthy,
):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    coordinator, store, provider = _coordinator(tmp_path, now)
    event = _event(now, **event_overrides)
    if healthy:
        _healthy(store, now)

    result = coordinator.execute_if_eligible(
        event=event,
        active_prediction=_active_prediction(**prediction_overrides),
        pointer_orders_enabled=pointer_enabled,
    )

    assert result is None
    assert provider.submits == []
    assert provider.lookups == []


def test_crash_after_reservation_queries_then_submits_same_id(tmp_path):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    coordinator, store, provider = _coordinator(tmp_path, now)
    event = _event(now)
    assert store.insert_event(event)
    store.reserve_execution(
        event_id=event["event_id"],
        provider="mt5",
        account_key="single-account",
        request_id=event["event_id"],
    )
    provider.lookup_results = [ProviderResult(ExecutionState.NOT_FOUND)]

    reconciled = coordinator.reconcile_outstanding()

    assert [row["state"] for row in reconciled] == ["succeeded"]
    assert provider.lookups == [event["event_id"]]
    assert [order.request_id for order in provider.submits] == [event["event_id"]]


def test_stale_reserved_attempt_is_not_submitted_when_remote_has_no_record(tmp_path):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    coordinator, store, provider = _coordinator(tmp_path, now)
    event = _event(now, signal_ts=now - timedelta(hours=1))
    assert store.insert_event(event)
    store.reserve_execution(
        event_id=event["event_id"],
        provider="mt5",
        account_key="single-account",
        request_id=event["event_id"],
    )
    provider.lookup_results = [ProviderResult(ExecutionState.NOT_FOUND)]

    reconciled = coordinator.reconcile_outstanding()

    assert reconciled[0]["state"] == "rejected"
    assert reconciled[0]["error_reason"] == "event expired before remote submission"
    assert provider.submits == []


def test_execution_response_persistence_redacts_credential_shaped_fields(tmp_path):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    store = MetaShadowStore(tmp_path / "meta.sqlite3")
    event = _event(now)
    assert store.insert_event(event)
    store.reserve_execution(
        event_id=event["event_id"],
        provider="mt5",
        account_key="single-account",
        request_id=event["event_id"],
    )

    store.update_execution(
        event_id=event["event_id"],
        provider="mt5",
        account_key="single-account",
        state="unknown",
        response={
            "error": "ambiguous",
            "Authorization": "Bearer private",
            "nested": {"api_key": "private-key", "order_ticket": 123},
        },
        error_reason="ambiguous response",
    )
    attempt = store.execution_attempt(
        event_id=event["event_id"], provider="mt5", account_key="single-account"
    )

    assert attempt["response"]["Authorization"] == "[redacted]"
    assert attempt["response"]["nested"] == {
        "api_key": "[redacted]",
        "order_ticket": 123,
    }


def test_ctrader_202_is_polled_without_a_second_order(tmp_path):
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    operation_id = str(uuid4())
    transport = FakeTransport(
        [
            _http(202, {"operation_id": operation_id, "state": "pending", "targets": []}),
            _http(200, {"operation_id": operation_id, "state": "succeeded", "targets": []}),
        ]
    )
    config = _config("ctrader")
    provider = CTraderExecutionProvider(config, transport)
    coordinator, store, _ = _coordinator(tmp_path, now, provider=provider, config=config)
    event = _event(now, event_id=operation_id)
    assert store.insert_event(event)
    store.record_execution_heartbeat(provider="ctrader", healthy=True, reason=None, checked_at=now)

    pending = coordinator.execute_if_eligible(
        event=event,
        active_prediction=_active_prediction(),
        pointer_orders_enabled=True,
    )
    completed = coordinator.reconcile_outstanding()

    assert pending["state"] == "pending"
    assert completed[0]["state"] == "succeeded"
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1]["url"].endswith(f"/v1/operations/{operation_id}")
