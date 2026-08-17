"""Opt-in real-order acceptance test. It has an unconditional live-account fuse."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from config import load_settings
from ctrader.gateway import CTraderGateway
from execution_repository import ExecutionRepository
from execution_service import ExecutionService
from models import (
    AmendOrderRequest,
    CancelOrderRequest,
    ClosePositionRequest,
    OperationState,
    OrderRequest,
    PositionProtectionRequest,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CTRADER_EXECUTION_INTEGRATION") != "1",
        reason="set CTRADER_EXECUTION_INTEGRATION=1 to place demo orders",
    ),
]

PROFILE = os.environ.get("CTRADER_PROFILE", "production")


def _price(value: Decimal, digits: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)


async def _wait_for_tick(gateway: CTraderGateway, account: str, instrument: str):
    deadline = asyncio.get_running_loop().time() + 60
    state = gateway.account(account)
    while asyncio.get_running_loop().time() < deadline:
        tick = state.hub.last_tick(instrument)
        if tick is not None:
            return tick
        await asyncio.sleep(0.25)
    pytest.fail(f"no demo tick received for {account}/{instrument}")


async def test_full_tiny_demo_lifecycle_on_every_enabled_account(tmp_path: Path) -> None:
    loaded = load_settings(PROFILE)
    if not loaded.gateway_enabled:
        pytest.fail("execution integration requires ACCOUNTS_CONFIG_PATH")
    live_aliases = [
        account.alias for account in loaded.enabled_accounts if account.environment == "live"
    ]
    if live_aliases:
        pytest.fail(f"live-account fuse: disable these accounts first: {live_aliases}")
    if not loaded.trading_enabled:
        pytest.fail("set TRADING_ENABLED=true only after confirming every enabled account is demo")
    if loaded.live_trading_enabled:
        pytest.fail("live-account fuse: LIVE_TRADING_ENABLED must be false")

    settings = loaded.model_copy(
        update={"execution_database_path": tmp_path / "demo-execution.sqlite3"}
    )
    repository = ExecutionRepository(settings.execution_database_path)
    repository.initialize()
    gateway = CTraderGateway(settings)
    service = ExecutionService(settings, gateway, repository)
    source = sorted(settings.allowed_order_sources)[0]
    await gateway.start()
    assert await gateway.wait_ready(45), "gateway did not authenticate and reconcile in 45s"

    try:
        for alias in gateway.aliases():
            account = gateway.account(alias)
            assert account.catalog is not None
            instrument = account.catalog.names()[0]
            symbol = account.catalog.info(instrument)
            assert symbol.lot_size and symbol.min_volume
            volume_lots = Decimal(symbol.min_volume) / Decimal(symbol.lot_size)
            assert settings.max_volume_lots is not None
            assert volume_lots <= settings.max_volume_lots
            tick = await _wait_for_tick(gateway, alias, instrument)

            before_positions = {position.position_id for position in gateway.list_positions(alias)}
            before_orders = {order.order_id for order in gateway.list_orders(alias)}
            created_positions: set[int] = set()
            created_orders: set[int] = set()
            try:
                opened = await service.place_order(
                    OrderRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        instrument=instrument,
                        execution_type="market",
                        direction="buy",
                        targets=[{"account": alias, "volume_lots": volume_lots}],
                    )
                )
                assert opened.state is OperationState.SUCCEEDED
                position_id = opened.targets[0].position_id
                assert position_id is not None
                created_positions.add(position_id)

                bid = Decimal(str(tick.bid))
                ask = Decimal(str(tick.ask))
                protected = await service.amend_position(
                    PositionProtectionRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        targets=[
                            {
                                "account": alias,
                                "position_id": position_id,
                                "stop_loss": _price(bid * Decimal("0.95"), symbol.digits),
                                "take_profit": _price(ask * Decimal("1.05"), symbol.digits),
                            }
                        ],
                    )
                )
                assert protected.state is OperationState.SUCCEEDED

                closed = await service.close_position(
                    ClosePositionRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        targets=[
                            {
                                "account": alias,
                                "position_id": position_id,
                                "volume_lots": volume_lots,
                            }
                        ],
                    )
                )
                assert closed.state is OperationState.SUCCEEDED
                created_positions.discard(position_id)

                pending = await service.place_order(
                    OrderRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        instrument=instrument,
                        execution_type="limit",
                        direction="buy",
                        entry_price=_price(bid * Decimal("0.80"), symbol.digits),
                        time_in_force="gtd",
                        expires_at=datetime.now(UTC) + timedelta(minutes=10),
                        targets=[{"account": alias, "volume_lots": volume_lots}],
                    )
                )
                assert pending.state is OperationState.SUCCEEDED
                order_id = pending.targets[0].order_id
                assert order_id is not None
                created_orders.add(order_id)

                # Restart with the pending order open: startup reconciliation must adopt it.
                await gateway.close()
                await gateway.start()
                assert await gateway.wait_ready(45)
                assert order_id in {order.order_id for order in gateway.list_orders(alias)}

                amended = await service.amend_order(
                    AmendOrderRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        targets=[
                            {
                                "account": alias,
                                "order_id": order_id,
                                "entry_price": _price(bid * Decimal("0.79"), symbol.digits),
                            }
                        ],
                    )
                )
                assert amended.state is OperationState.SUCCEEDED
                cancelled = await service.cancel_order(
                    CancelOrderRequest(
                        operation_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        source=source,
                        targets=[{"account": alias, "order_id": order_id}],
                    )
                )
                assert cancelled.state is OperationState.SUCCEEDED
                created_orders.discard(order_id)
            finally:
                # The fuse is demo-only, but cleanup is still mandatory.
                for order in gateway.list_orders(alias):
                    if order.order_id in created_orders and order.order_id not in before_orders:
                        await service.cancel_order(
                            CancelOrderRequest(
                                operation_id=uuid4(),
                                occurred_at=datetime.now(UTC),
                                source=source,
                                targets=[{"account": alias, "order_id": order.order_id}],
                            )
                        )
                for position in gateway.list_positions(alias):
                    if (
                        position.position_id in created_positions
                        and position.position_id not in before_positions
                        and position.volume_lots is not None
                    ):
                        await service.close_position(
                            ClosePositionRequest(
                                operation_id=uuid4(),
                                occurred_at=datetime.now(UTC),
                                source=source,
                                targets=[
                                    {
                                        "account": alias,
                                        "position_id": position.position_id,
                                        "volume_lots": position.volume_lots,
                                    }
                                ],
                            )
                        )
                assert not (
                    {order.order_id for order in gateway.list_orders(alias)} - before_orders
                )
                assert not (
                    {position.position_id for position in gateway.list_positions(alias)}
                    - before_positions
                )
    finally:
        await gateway.close()
