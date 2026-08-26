from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MethodType
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from ta_contracts import (
    OperationAction,
    OperationState,
    OrderRequest,
    SymbolInfo,
    TargetState,
)
from ta_store import ExecutionRepository

from execution_service.adapters.ctrader._generated.OpenApiModelMessages_pb2 import (
    ProtoOADeal,
    ProtoOAOrder,
    ProtoOATradeData,
)
from execution_service.adapters.ctrader.gateway import CTraderGateway
from execution_service.adapters.ctrader.proto import (
    ProtoOAAccountAuthReq,
    ProtoOAExecutionEvent,
    ProtoOAExecutionType,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAOrderType,
    ProtoOASubscribeSpotsReq,
    ProtoOATrader,
    ProtoOATraderReq,
    ProtoOATradeSide,
)
from execution_service.adapters.ctrader.symbols import SymbolCatalog
from execution_service.api import create_app
from execution_service.config import AccountDefinition
from execution_service.errors import CTraderError, ServiceError
from execution_service.service import ExecutionService
from tests.conftest import build_settings


def _gateway_settings(tmp_path: Path, **overrides: object):
    account = AccountDefinition(
        alias="forex_demo",
        ctid_trader_account_id=12345678,
        environment="demo",
        instruments={"EURUSD": "EURUSD"},
    )
    values: dict[str, object] = {
        "ACCOUNTS_CONFIG_PATH": tmp_path / "accounts.toml",
        "MAX_VOLUME_LOTS": "1.00",
        "ALLOWED_ORDER_SOURCES": "strategy_a",
        "TRADING_ENABLED": True,
        "accounts": (account,),
        "default_market_data_account": "forex_demo",
    }
    values.update(overrides)
    settings = build_settings(tmp_path, **values)
    return settings.model_copy(update={"accounts": (account,)})


def _ready_gateway(tmp_path: Path, **settings_overrides: object) -> CTraderGateway:
    gateway = CTraderGateway(_gateway_settings(tmp_path, **settings_overrides))
    account = gateway.account("forex_demo")
    account.catalog = SymbolCatalog(
        [
            SymbolInfo(
                symbol="EURUSD",
                symbol_id=1,
                digits=5,
                enabled=True,
                lot_size=10_000_000,
                min_volume=100_000,
                max_volume=100_000_000,
                step_volume=100_000,
                trading_mode=0,
            )
        ]
    )
    account.trader = ProtoOATrader(
        ctidTraderAccountId=12345678,
        balance=100_000,
        depositAssetId=1,
        accessRights=0,
        isLimitedRisk=False,
    )
    account.reconciled = True
    gateway._environment_ready["demo"].set()
    return gateway


def _production_gateway(tmp_path: Path) -> CTraderGateway:
    accounts = (
        AccountDefinition(
            alias="configured_demo",
            ctid_trader_account_id=1001,
            environment="demo",
            enabled=True,
            instruments={"EURUSD": "EURUSD"},
        ),
        AccountDefinition(
            alias="configured_live",
            ctid_trader_account_id=2001,
            environment="demo",
            enabled=False,
            instruments={"EURUSD": "EURUSD"},
        ),
        AccountDefinition(
            alias="stale",
            ctid_trader_account_id=3001,
            environment="demo",
            enabled=True,
            instruments={"EURUSD": "EURUSD"},
        ),
    )
    settings = build_settings(
        tmp_path,
        ACCOUNTS_CONFIG_PATH=tmp_path / "accounts.toml",
        MAX_VOLUME_LOTS="1.00",
        ALLOWED_ORDER_SOURCES="strategy_a",
        TRADING_ENABLED=True,
        profile="production",
        accounts=accounts,
        default_market_data_account="stale",
    )
    return CTraderGateway(settings)


def test_production_reconciles_all_authorized_accounts_and_broker_environments(
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)
    assert gateway.aliases() == ("configured_demo", "configured_live", "stale")

    gateway._reconcile_production_accounts({1001: False, 2001: True, 9999: True})

    assert gateway.aliases() == ("configured_demo", "configured_live")
    assert gateway.account("1001").definition.alias == "configured_demo"
    assert gateway.account("2001").definition.environment == "live"
    assert gateway.default_account_alias == "configured_demo"
    assert gateway.unconfigured_authorized_account_count == 1


def test_production_reconciliation_requires_an_authorized_registry_match(tmp_path: Path) -> None:
    gateway = _production_gateway(tmp_path)

    with pytest.raises(CTraderError, match="production registry"):
        gateway._reconcile_production_accounts({9999: False})


async def test_production_skips_disabled_account_without_losing_usable_environment(
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)
    disabled = AccountDefinition(
        alias="disabled_live",
        ctid_trader_account_id=2002,
        environment="live",
        enabled=False,
        instruments={"EURUSD": "EURUSD"},
    )
    gateway.settings = gateway.settings.model_copy(
        update={"accounts": (*gateway.settings.accounts, disabled)}
    )

    class BrokerClient:
        async def request(self, message):
            if isinstance(message, ProtoOAGetAccountListByAccessTokenReq):
                return type(
                    "Discovered",
                    (),
                    {
                        "ctidTraderAccount": (
                            type(
                                "DiscoveredAccount",
                                (),
                                {"ctidTraderAccountId": 2001, "isLive": True},
                            )(),
                            type(
                                "DiscoveredAccount",
                                (),
                                {"ctidTraderAccountId": 2002, "isLive": True},
                            )(),
                        )
                    },
                )()
            if isinstance(message, ProtoOAAccountAuthReq):
                if int(message.ctidTraderAccountId) == 2002:
                    raise CTraderError("RET_ACCOUNT_DISABLED", "Authentication failed")
                return object()
            if isinstance(message, ProtoOATraderReq):
                return type(
                    "TraderResponse",
                    (),
                    {
                        "trader": ProtoOATrader(
                            ctidTraderAccountId=2001,
                            balance=100_000,
                            depositAssetId=1,
                            accessRights=0,
                        )
                    },
                )()
            if isinstance(message, ProtoOASubscribeSpotsReq):
                return object()
            raise AssertionError(type(message).__name__)

    async def load_catalog(_self, _client, _account):
        return SymbolCatalog(
            [
                SymbolInfo(
                    symbol="EURUSD",
                    symbol_id=1,
                    digits=5,
                    enabled=True,
                    lot_size=10_000_000,
                    min_volume=100_000,
                    max_volume=100_000_000,
                    step_volume=100_000,
                    trading_mode=0,
                )
            ]
        )

    async def reconcile(_self, _client, account):
        account.reconciled = True

    gateway._load_catalog = MethodType(load_catalog, gateway)
    gateway._reconcile_account = MethodType(reconcile, gateway)

    await gateway._authenticate_and_load("live", BrokerClient())

    assert gateway.aliases() == ("configured_live",)
    assert gateway.account("configured_live").reconciled is True
    assert gateway.unavailable_authorized_account_count == 1
    with pytest.raises(KeyError, match="unknown or disabled"):
        gateway.account("disabled_live")

    # A reconnect in the other environment must not re-add the rejected live account.
    gateway._reconcile_production_accounts({2001: True, 2002: True})
    assert gateway.aliases() == ("configured_live",)


def _market_request(**changes: object) -> OrderRequest:
    values: dict[str, object] = {
        "operation_id": uuid4(),
        "occurred_at": datetime.now(UTC),
        "source": "strategy_a",
        "instrument": "EURUSD",
        "execution_type": "market",
        "direction": "buy",
        "targets": [{"account": "forex_demo", "volume_lots": "0.01"}],
        "stop_loss_distance": "0.001",
    }
    values.update(changes)
    return OrderRequest.model_validate(values)


async def test_market_order_is_converted_and_persisted(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)
    captured = []

    async def request(_self, account_alias, message, correlation_id=None):
        captured.append((account_alias, message))
        return ProtoOAExecutionEvent(
            ctidTraderAccountId=12345678,
            executionType=ProtoOAExecutionType.Value("ORDER_FILLED"),
            order=ProtoOAOrder(
                orderId=71,
                tradeData=ProtoOATradeData(
                    symbolId=1,
                    volume=100_000,
                    tradeSide=ProtoOATradeSide.Value("BUY"),
                ),
                orderType=ProtoOAOrderType.Value("MARKET"),
                orderStatus=2,
                clientOrderId=message.clientOrderId,
                executionPrice=1.085,
            ),
            deal=ProtoOADeal(
                dealId=81,
                orderId=71,
                positionId=91,
                volume=100_000,
                filledVolume=100_000,
                symbolId=1,
                createTimestamp=1,
                executionTimestamp=1,
                utcLastUpdateTimestamp=1,
                executionPrice=1.085,
                tradeSide=ProtoOATradeSide.Value("BUY"),
                dealStatus=2,
            ),
        )

    gateway.request = MethodType(request, gateway)
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    service = ExecutionService(gateway.settings, gateway, repository)
    request_model = _market_request()

    response = await service.place_order(request_model)

    assert response.state is OperationState.SUCCEEDED
    assert response.targets[0].state is TargetState.FILLED
    assert response.targets[0].executed_volume_lots == Decimal("0.01")
    sent = captured[0][1]
    assert sent.volume == 100_000
    assert sent.relativeStopLoss == 100
    assert len(sent.clientOrderId) <= 50


async def test_replay_does_not_send_a_second_order(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)
    calls = 0

    async def request(_self, _account_alias, message, correlation_id=None):
        nonlocal calls
        calls += 1
        return ProtoOAExecutionEvent(
            ctidTraderAccountId=12345678,
            executionType=ProtoOAExecutionType.Value("ORDER_ACCEPTED"),
            order=ProtoOAOrder(
                orderId=1,
                tradeData=ProtoOATradeData(symbolId=1, volume=100_000, tradeSide=1),
                orderType=ProtoOAOrderType.Value("LIMIT"),
                orderStatus=1,
                clientOrderId=message.clientOrderId,
            ),
        )

    gateway.request = MethodType(request, gateway)
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    service = ExecutionService(gateway.settings, gateway, repository)
    request_model = _market_request(
        execution_type="limit",
        entry_price="1.00",
        stop_loss_distance=None,
    )

    first = await service.place_order(request_model)
    replay = await service.place_order(request_model)

    assert replay == first
    assert calls == 1


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"source": "unknown"}, "source_not_allowed"),
        ({"occurred_at": datetime.now(UTC) - timedelta(minutes=5)}, "operation_too_old"),
        ({"targets": [{"account": "forex_demo", "volume_lots": "2"}]}, "volume_exceeds_limit"),
    ],
)
async def test_global_execution_guards(
    tmp_path: Path, changes: dict[str, object], error_code: str
) -> None:
    gateway = _ready_gateway(tmp_path)
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    service = ExecutionService(gateway.settings, gateway, repository)

    with pytest.raises(ServiceError) as exc_info:
        await service.place_order(_market_request(**changes))

    assert exc_info.value.code == error_code


def test_gateway_api_exposes_hybrid_operation_and_status_routes(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)

    async def start(_self):
        return None

    async def wait_ready(_self, timeout_seconds):
        return True

    async def close(_self):
        return None

    async def request(_self, _account_alias, message, correlation_id=None):
        return ProtoOAExecutionEvent(
            ctidTraderAccountId=12345678,
            executionType=ProtoOAExecutionType.Value("ORDER_FILLED"),
            order=ProtoOAOrder(
                orderId=71,
                tradeData=ProtoOATradeData(symbolId=1, volume=100_000, tradeSide=1),
                orderType=ProtoOAOrderType.Value("MARKET"),
                orderStatus=2,
                clientOrderId=message.clientOrderId,
            ),
        )

    gateway.start = MethodType(start, gateway)
    gateway.wait_ready = MethodType(wait_ready, gateway)
    gateway.close = MethodType(close, gateway)
    gateway.request = MethodType(request, gateway)
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    app = create_app(gateway.settings, gateway=gateway, repository=repository)
    payload = _market_request(stop_loss_distance=None).model_dump(mode="json")

    with TestClient(app) as client:
        created = client.post(
            "/v1/orders",
            headers={"X-API-Key": "test-api-key-at-least-16"},
            json=payload,
        )
        fetched = client.get(
            f"/v1/operations/{payload['operation_id']}",
            headers={"X-API-Key": "test-api-key-at-least-16"},
        )
        accounts = client.get(
            "/v1/accounts",
            headers={"X-API-Key": "test-api-key-at-least-16"},
        )
        orders_by_id = client.get(
            "/v1/accounts/12345678/orders",
            headers={"X-API-Key": "test-api-key-at-least-16"},
        )

    assert created.status_code == 201
    assert created.json()["state"] == "succeeded"
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert accounts.status_code == 200
    assert accounts.json()["accounts"] == [
        {
            "alias": "forex_demo",
            "ctid_trader_account_id": 12345678,
            "environment": "demo",
            "is_live": False,
            "connected": True,
            "reconciled": True,
            "broker_access_rights": "full_access",
            "available_for_trading": True,
            "order_entry_enabled": True,
            "position_close_enabled": True,
        }
    ]
    assert accounts.json()["unavailable_authorized_accounts"] == 0
    assert orders_by_id.status_code == 200
    assert orders_by_id.json() == []


def test_followup_mutation_event_uses_envelope_correlation_not_original_order_id(
    tmp_path: Path,
) -> None:
    gateway = _ready_gateway(tmp_path)
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    service = ExecutionService(gateway.settings, gateway, repository)
    place_id = uuid4()
    cancel_id = uuid4()
    repository.reserve(
        operation_id=place_id,
        action=OperationAction.PLACE_ORDER,
        source="strategy_a",
        payload_hash="place",
        payload_json="{}",
        targets=[("forex_demo", "original-client-order")],
    )
    repository.reserve(
        operation_id=cancel_id,
        action=OperationAction.CANCEL_ORDER,
        source="strategy_a",
        payload_hash="cancel",
        payload_json="{}",
        targets=[("forex_demo", "cancel-correlation")],
    )
    event = ProtoOAExecutionEvent(
        ctidTraderAccountId=12345678,
        executionType=ProtoOAExecutionType.Value("ORDER_CANCELLED"),
        order=ProtoOAOrder(
            orderId=71,
            tradeData=ProtoOATradeData(symbolId=1, volume=100_000, tradeSide=1),
            orderType=ProtoOAOrderType.Value("LIMIT"),
            orderStatus=3,
            clientOrderId="original-client-order",
        ),
    )

    service._on_execution_event("forex_demo", event, "cancel-correlation")

    assert repository.get(cancel_id).targets[0].state is TargetState.CANCELLED
    assert repository.get(place_id).targets[0].state is TargetState.RESERVED
