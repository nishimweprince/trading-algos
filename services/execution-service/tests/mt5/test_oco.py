from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from ta_core import ServiceError

from execution_service.adapters.mt5.oco_models import OcoGroupRequest
from execution_service.adapters.mt5.oco_repository import OcoRepository
from execution_service.adapters.mt5.oco_service import Mt5OcoService
from execution_service.adapters.mt5.service import SignalExecutionService

from .fakes import FakeMT5Adapter


class OcoAdapter(FakeMT5Adapter):
    def __init__(self, login: int) -> None:
        super().__init__()
        self.connection = replace(self.connection, login=login)
        self.symbol = replace(self.symbol, expiration_mode=4)
        self.login = login
        self.margin_mode = 2
        self.live_orders: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.next_ticket = 1001
        self.reject_second = False
        self.raise_after_first = False
        self.fail_protection = False
        self.fill_during_cancel = False
        self.cancel_unknown = False
        self.server_offset = 0

    def symbol_tick(self, symbol: str) -> Any:
        return replace(
            super().symbol_tick(symbol),
            time=int(datetime.now(UTC).timestamp()) + self.server_offset,
        )

    def account_metadata(self) -> dict[str, Any]:
        return {"login": self.login, "margin_mode": self.margin_mode, "server": "Broker-Demo"}

    def active_orders(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.live_orders]

    def active_positions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.positions]

    def order_send(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.send_requests.append(dict(request))
        if request["action"] == self.constants.trade_action_pending:
            ticket = self.next_ticket
            self.next_ticket += 1
            if ticket == 1002 and self.reject_second:
                return {"retcode": 10013}
            self.live_orders.append(
                {
                    **request,
                    "ticket": ticket,
                    "state": 1,
                    "price_open": request["price"],
                    "volume_current": request["volume"],
                }
            )
            if ticket == 1001 and self.raise_after_first:
                raise RuntimeError("response lost after acceptance")
            return {"retcode": 10008, "order": ticket}
        if request["action"] == self.constants.trade_action_remove:
            ticket = request["order"]
            order = next((row for row in self.live_orders if row["ticket"] == ticket), None)
            if self.cancel_unknown:
                return None
            if order is not None:
                if self.fill_during_cancel:
                    self.fill_during_cancel = False
                    self.fill(ticket, order["price"])
                else:
                    self.live_orders.remove(order)
                    self.orders.append({**order, "state": 2})
            return {"retcode": 10009}
        if request["action"] == self.constants.trade_action_sltp:
            if self.fail_protection:
                return None
            position = next(row for row in self.positions if row["ticket"] == request["position"])
            position.update(sl=request["sl"], tp=request["tp"])
            return {"retcode": 10009}
        if request["action"] == self.constants.trade_action_deal:
            position = next(row for row in self.positions if row["ticket"] == request["position"])
            self.positions.remove(position)
            self.deals.append(
                {
                    **position,
                    "position_id": position["ticket"],
                    "entry": 1,
                    "order": 3000,
                    "price": request["price"],
                }
            )
            return {"retcode": 10009}
        raise AssertionError(request)

    def fill(self, ticket: int, price: float, *, volume: float = 0.1, time_msc: int = 1000) -> None:
        order = next(row for row in self.live_orders if row["ticket"] == ticket)
        if volume == order["volume_current"]:
            self.live_orders.remove(order)
            self.orders.append({**order, "state": 4})
        else:
            order["volume_current"] -= volume
        position_id = ticket + 1000
        self.positions.append(
            {
                "ticket": position_id,
                "identifier": position_id,
                "magic": order["magic"],
                "symbol": order["symbol"],
                "type": 0 if order["type"] == 4 else 1,
                "volume": volume,
                "sl": order["sl"],
                "tp": order["tp"],
            }
        )
        self.deals.append(
            {
                "order": ticket,
                "position_id": position_id,
                "entry": 0,
                "magic": order["magic"],
                "symbol": order["symbol"],
                "price": price,
                "volume": volume,
                "time_msc": time_msc,
            }
        )


def request(settings: Any, **overrides: Any) -> OcoGroupRequest:
    now = datetime.now(UTC)
    return OcoGroupRequest(
        group_id=uuid4(),
        profile="hfm",
        occurred_at=now,
        decision_at=now,
        symbol="EURUSD",
        volume="0.1",
        upper_trigger="1.101",
        lower_trigger="1.099",
        stop_distance="0.001",
        target_distance="0.002",
        expires_at=now + timedelta(minutes=15),
        source="trading_central",
        **overrides,
    )


async def setup(settings: Any) -> tuple[Mt5OcoService, OcoAdapter]:
    settings = settings.model_copy(update={"profile": "hfm", "mt5_oco_enabled": True})
    adapter = OcoAdapter(settings.login)
    repository = OcoRepository(settings.database_path.with_suffix(".oco.sqlite3"))
    repository.initialize()
    from execution_service.adapters.mt5.legacy_repository import SignalRepository

    signals = SignalExecutionService(settings, adapter, SignalRepository(settings.database_path))
    coordinator = Mt5OcoService(signals, repository)
    await coordinator.monitor_once(startup=True)
    return coordinator, adapter


@pytest.mark.asyncio
async def test_server_clock_offset_applies_only_at_broker_boundary(settings: Any) -> None:
    service, adapter = await setup(settings)
    service.settings.mt5_oco_server_utc_offset_seconds = 10800
    adapter.server_offset = 10800
    payload = request(settings)
    group = await service.submit(payload)
    assert group["state"] == "placed"
    assert group["expires_at"] == payload.expires_at.isoformat().replace("+00:00", "Z")
    assert group["server_utc_offset_seconds"] == 10800
    for row in adapter.send_requests:
        assert row["expiration"] == int(payload.expires_at.timestamp()) + 10800
    utc_fill = int(datetime.now(UTC).timestamp()) * 1000
    adapter.fill(1001, 1.1015, time_msc=utc_fill + 10800000)
    await service.monitor_once()
    group = await service.get(payload.group_id)
    assert group["legs"]["long"]["filled_at_msc"] == utc_fill


@pytest.mark.asyncio
async def test_unverified_server_clock_blocks_new_entries(settings: Any) -> None:
    service, adapter = await setup(settings)
    adapter.server_offset = 10800
    with pytest.raises(ServiceError) as error:
        await service.submit(request(settings))
    assert error.value.code == "oco_server_clock_unverified"
    assert not adapter.send_requests


@pytest.mark.asyncio
async def test_oco_placement_and_first_fill_cancel(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    assert group["state"] == "placed"
    assert len(adapter.live_orders) == 2
    adapter.fill(1001, 1.1015)
    await service.monitor_once()
    assert not adapter.live_orders
    await service.monitor_once()
    group = await service.get(group["group_id"])
    assert group["winner"] == "long"
    assert group["legs"]["short"]["state"] == "cancelled"
    assert adapter.positions[0]["sl"] == 1.1005
    assert adapter.positions[0]["tp"] == 1.1035
    assert group["legs"]["long"]["applied_protection"]["anchor"] == "actual_fill"


@pytest.mark.asyncio
async def test_oco_idempotency_and_changed_body(settings: Any) -> None:
    service, adapter = await setup(settings)
    original = request(settings)
    await service.submit(original)
    await service.submit(original)
    assert len(adapter.live_orders) == 2
    with pytest.raises(ServiceError, match="different payload"):
        await service.submit(original.model_copy(update={"source": "ipda"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_oco_incomplete_placement_cancels_surviving_leg(
    settings: Any, lost_response: bool
) -> None:
    service, adapter = await setup(settings)
    adapter.reject_second = not lost_response
    adapter.raise_after_first = lost_response
    group = await service.submit(request(settings))
    await service.monitor_once(startup=True)
    await service.monitor_once()
    assert not adapter.live_orders
    assert len([row for row in adapter.send_requests if row["action"] == 5]) == (
        1 if lost_response else 2
    )
    assert not service._uncertain(await service.get(group["group_id"]))


@pytest.mark.asyncio
async def test_oco_partial_fill_cancels_sibling_and_residual_without_topup(settings: Any) -> None:
    service, adapter = await setup(settings)
    await service.submit(request(settings))
    adapter.fill(1001, 1.101, volume=0.04)
    await service.monitor_once()
    assert not adapter.live_orders
    assert len(adapter.positions) == 1
    assert adapter.positions[0]["volume"] == 0.04
    assert len([row for row in adapter.send_requests if row["action"] == 5]) == 2


@pytest.mark.asyncio
async def test_oco_sibling_fill_during_cancel_closes_owned_loser_and_halts(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.fill(1001, 1.101, time_msc=500)
    adapter.fill_during_cancel = True
    await service.monitor_once()
    await service.monitor_once()
    await service.monitor_once()
    group = await service.get(group["group_id"])
    assert group["state"] == "halted"
    assert group["winner"] == "long"
    assert group["legs"]["short"]["state"] == "closed"
    assert [row["ticket"] for row in adapter.positions] == [2001]
    assert not (await service.capabilities())["ready"]


@pytest.mark.asyncio
async def test_oco_failed_postfill_protection_blocks_new_groups(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.fill(1001, 1.1015)
    adapter.fail_protection = True
    await service.monitor_once()
    group = await service.get(group["group_id"])
    assert group["fault"] == "fill_protection_unknown"
    with pytest.raises(ServiceError, match="Resolve existing"):
        await service.submit(request(settings))


@pytest.mark.asyncio
async def test_oco_cancel_unknown_keeps_resting_order_visible(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.cancel_unknown = True
    group = await service.cancel(group["group_id"], "engine_expiry")
    assert group["state"] == "cancelling"
    assert len(adapter.live_orders) == 2
    assert group["legs"]["long"]["state"] == "placed"


@pytest.mark.asyncio
async def test_oco_rejects_account_mode_and_unsupported_expiry(settings: Any) -> None:
    service, adapter = await setup(settings)
    adapter.margin_mode = 0
    with pytest.raises(ServiceError, match="hedge account"):
        await service.submit(request(settings))
    adapter.margin_mode = 2
    adapter.symbol = replace(adapter.symbol, expiration_mode=1)
    with pytest.raises(ServiceError, match="specified expiry"):
        await service.submit(request(settings))


@pytest.mark.asyncio
async def test_oco_restart_cancels_accepted_pending_leg_missing_from_history(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.live_orders = adapter.live_orders[:1]
    group["legs"]["short"].update(order_id=None, state="not_submitted")
    group["placement_complete"] = False
    service.repository.save(group)
    restarted = Mt5OcoService(service.signals, service.repository)
    await restarted.monitor_once(startup=True)
    await restarted.monitor_once()
    assert not adapter.live_orders
    assert len([row for row in adapter.send_requests if row["action"] == 5]) == 2


@pytest.mark.asyncio
async def test_oco_watchdog_expiry_and_offline_monitor(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    group["occurred_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    group["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    service.repository.save(group)
    adapter.connection = replace(adapter.connection, connected=False)
    await service.monitor_once()
    assert not (await service.capabilities())["ready"]
    assert len(adapter.live_orders) == 2
    adapter.connection = replace(adapter.connection, connected=True)
    await service.monitor_once()
    await service.monitor_once()
    assert not adapter.live_orders
    assert (await service.get(group["group_id"]))["state"] == "expired"


@pytest.mark.asyncio
async def test_oco_rejects_widened_protection_and_stale_decisions(settings: Any) -> None:
    service, adapter = await setup(settings)
    original = request(settings)
    widened = original.model_copy(update={"stop_distance": Decimal("0.00001")})
    assert (await service.submit(widened))["state"] == "rejected"
    stale = request(settings).model_copy(
        update={"decision_at": datetime.now(UTC) - timedelta(hours=2)}
    )
    assert (await service.submit(stale))["state"] == "rejected"
    assert not adapter.live_orders


@pytest.mark.asyncio
async def test_oco_manual_close_of_owned_position_is_accounted_without_touching_other_positions(
    settings: Any,
) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.fill(1001, 1.101)
    await service.monitor_once()
    adapter.positions.clear()
    adapter.deals.append(
        {
            "position_id": 2001,
            "entry": 1,
            "magic": 0,
            "symbol": "EURUSD",
            "volume": 0.1,
            "price": 1.102,
            "profit": 1.0,
            "commission": -0.2,
        }
    )
    unrelated = {"ticket": 9999, "identifier": 9999, "magic": 0, "symbol": "EURUSD", "volume": 5}
    adapter.positions.append(unrelated)
    await service.monitor_once()
    group = await service.get(group["group_id"])
    assert group["state"] == "closed"
    assert group["legs"]["long"]["accounting"]["realized_net_pnl"] == "0.8"
    assert adapter.positions == [unrelated]


def test_oco_authenticated_api_and_background_monitor(settings: Any) -> None:
    from execution_service.api import create_app

    settings = settings.model_copy(update={"profile": "hfm", "mt5_oco_enabled": True})
    adapter = OcoAdapter(settings.login)
    app = create_app(settings, mt5_adapter=adapter)
    headers = {"X-API-Key": settings.api_key.get_secret_value()}
    original = request(settings)
    with TestClient(app) as client:
        assert client.post("/v1/mt5/oco", json=original.model_dump(mode="json")).status_code == 401
        capability = client.get(
            "/v1/mt5/capabilities", params={"symbol": "EURUSD"}, headers=headers
        )
        assert capability.json()["ready"]
        response = client.post(
            "/v1/mt5/oco", json=original.model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 200
        assert response.json()["state"] == "placed"
        assert len(client.get("/v1/mt5/inventory", headers=headers).json()["orders"]) == 2
        cancel = client.post(f"/v1/mt5/oco/{original.group_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert not adapter.live_orders


@pytest.mark.asyncio
async def test_oco_incident_acknowledgement_requires_settlement_and_stays_acknowledged(
    settings: Any,
) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    adapter.fill(1001, 1.101, time_msc=500)
    adapter.fill(1002, 1.099, time_msc=600)
    await service.monitor_once()
    with pytest.raises(ServiceError, match="not settled"):
        await service.acknowledge_recovery(group["group_id"])
    await service.close_owned_group(group["group_id"])
    await service.monitor_once()
    assert not adapter.positions
    await service.acknowledge_recovery(group["group_id"])
    await service.monitor_once()
    assert (await service.capabilities())["ready"]


@pytest.mark.asyncio
async def test_oco_account_change_blocks_control_actions(settings: Any) -> None:
    service, adapter = await setup(settings)
    group = await service.submit(request(settings))
    before = len(adapter.send_requests)
    adapter.login += 1
    with pytest.raises(ServiceError, match="different account"):
        await service.cancel(group["group_id"], "operator")
    with pytest.raises(ServiceError, match="different account"):
        await service.close_owned_group(group["group_id"])
    assert len(adapter.send_requests) == before
