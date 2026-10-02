"""The execution bridge: shadow fills, testnet orders through a fake gateway, halts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from ta_clients.execution import ExecutionClient

from ofi_scalper_service.alerts import Alerts
from ofi_scalper_service.app import _ClientSettings
from ofi_scalper_service.execution_bridge import DEFAULT_FILTERS, ExecutionBridge
from ofi_scalper_service.model import load_model
from ofi_scalper_service.policy import Phase
from ofi_scalper_service.risk import HaltReason, RiskLimits, RiskState
from tests.conftest import RecordingNotifier, make_settings, until
from tests.fake_gateway import ACCOUNT, FakeGateway, FakeQuotes
from tests.model_fixture import Dial, dial_model

T0 = 1_790_000_000_000_000_000
S = 1_000_000_000
GATE_ON = {"on": True, "threshold_mult": 1.0, "max_inventory_mult": 1.0, "reasons": []}
FEES = (2.0, 5.0)


def sample(t: int, *, bid: float = 60000.0, ask: float = 60000.1, symbol: str = "BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "t_ns": t,
        "mid": (bid + ask) / 2,
        "best_bid": bid,
        "best_ask": ask,
        "ofi_int_1000ms": 1.0,
        "imbalance": 0.6,
        "microprice_minus_mid_bp": 0.1,
        "spread_bp": 0.02,
        "secs_to_funding": 3600.0,
        "funding_rate": 0.0001,
    }


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> int:
        self.now += 1_000_000
        return self.now


# --- shadow ------------------------------------------------------------------------------


def shadow_bridge(
    tmp_path: Path, dial: Dial, **overrides: Any
) -> tuple[ExecutionBridge, RiskState]:
    settings = make_settings(
        tmp_path,
        OFI_EXECUTION_MODE="shadow",
        OFI_MODEL_VERSION="v1",
        OFI_ORDER_NOTIONAL_USD=180,
        **overrides,
    )
    risk = RiskState(RiskLimits.from_settings(settings))
    bridge = ExecutionBridge(
        settings,
        risk=risk,
        alerts=Alerts(RecordingNotifier()),
        model=dial_model(tmp_path / "models", dial),
        filters=dict(DEFAULT_FILTERS),
        state_dir=tmp_path / "state",
        fees=lambda _s: FEES,
    )
    return bridge, risk


def test_shadow_maker_fills_only_on_a_print_through_the_price(tmp_path: Path) -> None:
    dial = Dial()
    bridge, risk = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    btc = bridge.policies["BTCUSDT"]
    assert btc.phase is Phase.ENTRY_RESTING
    (entry,) = bridge.orders.values()
    assert entry.price == "60000.0" and entry.qty == "0.003" and entry.post_only
    bridge.on_trade("BTCUSDT", T0 + 1, 60000.0)  # at the price: queue, not a fill
    assert btc.phase is Phase.ENTRY_RESTING
    bridge.on_trade("BTCUSDT", T0 + 2, 59999.9)  # through it
    assert btc.phase is Phase.OPEN
    assert risk.positions["BTCUSDT"] == pytest.approx(0.003 * 60000.0)
    tp = bridge.orders[f"{btc.cycle.cycle_id}|tp"]
    assert tp.price == "60048.0" and tp.reduce_only and tp.side == "sell"
    bridge.on_trade("BTCUSDT", T0 + 3 * S, 60048.1)
    assert btc.phase is Phase.FLAT
    dial.set()  # no further signal
    for second in (1, 5, 30):  # adverse-selection marks after the entry fill
        bridge.on_sample(sample(T0 + second * S + 2, bid=60010.0, ask=60010.1), GATE_ON)
    (trade,) = bridge.book.recent_trades
    assert trade["exit_reason"] == "take_profit" and trade["filled"]
    # 0.003 x 48 = 0.144 gross; fees 2 bp on each leg
    assert trade["gross_usd"] == pytest.approx(0.144)
    assert trade["fees_usd"] == pytest.approx(0.003 * (60000 + 60048) * 2 / 10_000, abs=1e-6)
    assert trade["adverse_bp"]["1s"] == pytest.approx((60010.05 - 60000) / 60000 * 10_000, abs=1e-3)
    assert bridge.realized_usd == pytest.approx(trade["net_usd"])
    assert (tmp_path / "state" / "trades").is_dir()
    (signal,) = bridge.book.recent_signals
    assert signal["action"] == "enter" and signal["model_version"] == "v1"


def test_shadow_post_only_that_would_cross_is_rejected(tmp_path: Path) -> None:
    dial = Dial()
    bridge, _ = shadow_bridge(tmp_path, dial)
    bridge.shadow.touch["BTCUSDT"] = (60000.0, 60000.0)  # locked book
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0, bid=60000.1, ask=60000.0), GATE_ON)
    assert bridge.policies["BTCUSDT"].phase is Phase.FLAT
    assert bridge.counts["cycles_unfilled"] == 1


def test_shadow_stop_closes_at_the_bid(tmp_path: Path) -> None:
    dial = Dial()
    bridge, _ = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    bridge.on_trade("BTCUSDT", T0 + 1, 59999.0)
    dial.set()
    bridge.on_sample(sample(T0 + S, bid=59950.0, ask=59950.1), GATE_ON)
    assert bridge.policies["BTCUSDT"].phase is Phase.FLAT
    bridge.flush_pending()
    (trade,) = bridge.book.recent_trades
    assert trade["exit_reason"] == "stop" and trade["exit_price"] == 59950.0
    assert trade["legs"]["close1"]["liquidity"] == "taker"


def test_shadow_blocks_entries_without_gate_fees_or_while_paused(tmp_path: Path) -> None:
    dial = Dial()
    bridge, risk = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), None)
    risk.set_pause("BTCUSDT", "stale_book", True)
    bridge.on_sample(sample(T0 + 1), GATE_ON)
    risk.set_pause("BTCUSDT", "stale_book", False)
    bridge.on_sample(sample(T0 + 2), {**GATE_ON, "on": False, "reasons": ["funding_blackout"]})
    actions = [s["action"] for s in bridge.book.recent_signals]
    assert actions == ["skip:gate_pending", "skip:paused:stale_book", "skip:gate:funding_blackout"]
    assert not bridge.orders


def test_shadow_kill_closes_open_positions(tmp_path: Path) -> None:
    dial = Dial()
    bridge, risk = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    bridge.on_trade("BTCUSDT", T0 + 1, 59999.0)
    risk.kill("http")
    bridge.on_halt("kill_switch", T0 + 2)
    assert bridge.policies["BTCUSDT"].phase is Phase.FLAT
    assert risk.positions["BTCUSDT"] == 0.0
    bridge.on_sample(sample(T0 + 3), GATE_ON)
    assert bridge.book.recent_signals[-1]["action"] == "skip:halted"


def test_shadow_daily_loss_halts_and_liquidates(tmp_path: Path) -> None:
    dial = Dial()
    bridge, risk = shadow_bridge(tmp_path, dial, OFI_DAILY_LOSS_LIMIT_USD=1)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    bridge.on_trade("BTCUSDT", T0 + 1, 59999.0)
    dial.set()
    # -0.3% on 0.003 BTC is ~ -$0.54; set the stop wide enough not to fire first.
    bridge.policies["BTCUSDT"].params = bridge.policies["BTCUSDT"].params.__class__(
        horizon_s=5, threshold=0.6, barrier_bp=500, buffer_bp=1
    )
    bridge.on_sample(sample(T0 + S, bid=59600.0, ask=59600.1), GATE_ON)
    assert risk.halt is not None and risk.halt.reason is HaltReason.DAILY_LOSS
    assert bridge.policies["BTCUSDT"].phase is Phase.FLAT


# --- testnet --------------------------------------------------------------------------


@pytest.fixture
async def testnet(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    made: list[tuple[ExecutionBridge, httpx.AsyncClient]] = []

    async def build(
        gateway: FakeGateway, *, dial: Dial | None = None, start: bool = True, **overrides: Any
    ) -> tuple[ExecutionBridge, RiskState]:
        values = {
            "OFI_EXECUTION_MODE": "testnet",
            "EXECUTION_API_KEY": "gateway-key-at-least-16",
            "OFI_ORDER_NOTIONAL_USD": 180,
            "OFI_FILL_POLL_MS": 20,
            **overrides,
        }
        if dial is not None:
            values["OFI_MODEL_VERSION"] = "v1"
        settings = make_settings(tmp_path, **values)
        http = httpx.AsyncClient(transport=httpx.MockTransport(gateway.handler))
        client = ExecutionClient(
            _ClientSettings(
                ctrader_markets_url="http://gateway.test",
                execution_timeout_seconds=1.0,
                ctrader_api_key=settings.execution_api_key,
                execution_account=ACCOUNT,
            ),
            http,
        )
        risk = RiskState(RiskLimits.from_settings(settings))
        models = tmp_path / "models"
        model = None
        if dial is not None:
            # A restart reloads the same pinned model.
            if (models / "v1").exists():
                model = load_model(models, "v1", backend=dial.backend)
            else:
                model = dial_model(models, dial)
        bridge = ExecutionBridge(
            settings,
            risk=risk,
            alerts=Alerts(RecordingNotifier()),
            model=model,
            filters=dict(DEFAULT_FILTERS),
            client=client,
            quotes=FakeQuotes(),
            state_dir=tmp_path / "state",
            clock_ns=Clock(),
            fees=lambda _s: FEES,
        )
        made.append((bridge, http))
        if start:
            await bridge.start()
            await until(lambda: bridge.venue_ready)
        return bridge, risk

    yield build
    for bridge, http in made:
        await bridge.close()
        await http.aclose()


async def test_testnet_full_cycle_through_the_gateway(testnet) -> None:
    gateway, dial = FakeGateway(), Dial()
    bridge, risk = await testnet(gateway, dial=dial)
    await until(lambda: any(p.endswith("/dead-man") for p in gateway.paths()))
    dead_man = next(body for _, path, body in gateway.calls if path.endswith("/dead-man"))
    assert dead_man == {"instruments": ["BTCUSDT", "ETHUSDT"], "countdown_ms": 15000}

    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: len(gateway.orders()) == 1)
    (entry,) = gateway.orders()
    assert entry["source"] == "ofi_scalper" and entry["execution_type"] == "limit"
    assert entry["post_only"] is True and "reduce_only" not in entry
    assert entry["entry_price"] == "60000.0"  # demo's touch from FakeQuotes
    assert entry["targets"] == [{"account": ACCOUNT, "volume_lots": "0.003"}]

    gateway.settle(entry["operation_id"], "filled", "0.003", "60000.0")
    await until(lambda: len(gateway.orders()) == 2)
    tp = gateway.payloads[gateway.op_for("tp")]
    assert tp["reduce_only"] is True and tp["post_only"] is True
    assert tp["direction"] == "sell" and tp["entry_price"] == "60048.0"
    assert risk.positions["BTCUSDT"] == pytest.approx(180.0)

    gateway.settle(tp["operation_id"], "filled", "0.003", "60048.0")
    await until(lambda: bridge.policies["BTCUSDT"].phase is Phase.FLAT)
    bridge.flush_pending()
    (trade,) = bridge.book.recent_trades
    assert trade["exit_reason"] == "take_profit" and trade["net_usd"] > 0
    assert "entry" in trade["rtt_ms"]
    assert risk.equity == pytest.approx(1000.0)  # wallet from /v1/accounts


async def test_a_lost_response_is_reconciled_never_resubmitted(testnet) -> None:
    gateway, dial = FakeGateway(submit_mode="drop"), Dial()
    bridge, _ = await testnet(gateway, dial=dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: bridge.consecutive_unknown == 1)
    gateway.submit_mode = "ok"
    (op,) = gateway.payloads
    await until(lambda: bridge.orders[next(iter(bridge.orders))].state == "placed")
    posts = [c for c in gateway.calls if c[1] == "/v1/orders"]
    assert len(posts) == 1  # reconciled through GET /v1/operations, not resent
    gateway.settle(op, "filled", "0.003", "60000.0")
    await until(lambda: bridge.policies["BTCUSDT"].phase is Phase.OPEN)


async def test_an_order_that_never_arrived_is_not_submitted(testnet) -> None:
    gateway, dial = FakeGateway(submit_mode="down"), Dial()
    bridge, _ = await testnet(gateway, dial=dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: bridge.policies["BTCUSDT"].phase is Phase.FLAT)
    (cycle,) = bridge.book.recent_trades
    assert not cycle["filled"]


async def test_consecutive_unknowns_halt_and_cancel_everything(testnet) -> None:
    gateway = FakeGateway()
    bridge, risk = await testnet(gateway)
    for n in range(3):
        bridge.note_unknown(f"test {n}")
    assert risk.halt is not None and risk.halt.reason is HaltReason.EXECUTION
    await until(lambda: any(p.endswith("/cancel-all") for p in gateway.paths()))
    await until(lambda: any(p.endswith("/flatten") for p in gateway.paths()), seconds=3)


async def test_kill_cancels_closes_then_flattens(testnet) -> None:
    gateway, dial = FakeGateway(), Dial()
    bridge, risk = await testnet(gateway, dial=dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: len(gateway.orders()) == 1)
    gateway.settle(gateway.orders()[0]["operation_id"], "filled", "0.003", "60000.0")
    await until(lambda: len(gateway.orders()) == 2)
    await until(
        lambda: (
            bridge.orders[f"{bridge.policies['BTCUSDT'].cycle.cycle_id}|tp"].order_id is not None
        )
    )
    start = len(gateway.calls)
    risk.kill("http")
    bridge.on_halt("kill_switch")
    await until(lambda: any(p.endswith("/flatten") for p in gateway.paths()[start:]), seconds=3)
    sequence = [
        p
        for p in gateway.paths()[start:]
        if p in {"/v1/orders", "/v1/orders/cancel"} or "/v1/accounts/" in p
    ]
    sequence = [p for p in sequence if not p.endswith("/dead-man") and not p.endswith("/positions")]
    assert sequence[0].endswith("/cancel-all")
    assert sequence[-1].endswith("/flatten")
    close = gateway.payloads[gateway.op_for("close1")]
    assert close["execution_type"] == "market" and close["reduce_only"] is True
    assert "post_only" not in close


async def test_restart_reconciles_the_persisted_order(testnet, tmp_path: Path) -> None:
    gateway, dial = FakeGateway(), Dial()
    bridge, _ = await testnet(gateway, dial=dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: len(gateway.orders()) == 1 and next(iter(bridge.orders.values())).order_id)
    await bridge.close()
    assert (tmp_path / "state" / "bridge.json").is_file()

    gateway.settle(gateway.orders()[0]["operation_id"], "filled", "0.003", "60000.0")
    gateway.positions = [{"instrument": "BTCUSDT", "volume_lots": "0.003", "direction": "buy"}]
    again, risk = await testnet(gateway, dial=dial)
    # The restored cycle learnt of its fill through the gateway and placed its TP;
    # the position matches, so nothing halted and nothing was resubmitted.
    await until(lambda: again.policies["BTCUSDT"].phase is Phase.OPEN)
    assert not risk.halted and again.position_mismatch is None
    posts = [c for c in gateway.calls if c[1] == "/v1/orders"]
    assert [p[2]["note"].rsplit("|", 1)[1] for p in posts] == ["entry", "tp"]


async def test_a_position_the_bridge_did_not_open_halts_without_flattening(testnet) -> None:
    gateway = FakeGateway(
        positions=[{"instrument": "ETHUSDT", "volume_lots": "0.5", "direction": "sell"}]
    )
    bridge, risk = await testnet(gateway, dial=Dial())
    assert risk.halt is not None and risk.halt.reason is HaltReason.EXECUTION
    assert "ETHUSDT" in (bridge.position_mismatch or "")
    bridge.on_sample(sample(T0), GATE_ON)
    await until(lambda: any(p.endswith("/cancel-all") for p in gateway.paths()))
    assert not any(p.endswith("/flatten") for p in gateway.paths())


async def test_controls_only_without_a_model(testnet) -> None:
    gateway = FakeGateway()
    bridge, risk = await testnet(gateway)
    assert bridge.policies == {}
    bridge.on_sample(sample(T0), GATE_ON)
    assert not gateway.orders()
    risk.kill("file")
    bridge.on_halt("kill_switch")
    await until(lambda: any(p.endswith("/flatten") for p in gateway.paths()), seconds=3)
    assert any(p.endswith("/cancel-all") for p in gateway.paths())


async def test_gateway_not_ready_blocks_entries(testnet) -> None:
    gateway, dial = FakeGateway(ready=False), Dial()
    bridge, _ = await testnet(gateway, dial=dial, start=False)
    await bridge.start()
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    assert bridge.book.recent_signals[-1]["action"] == "skip:venue_not_ready"
    assert not gateway.orders()


def test_payload_is_a_valid_order_request(tmp_path: Path) -> None:
    dial = Dial()
    bridge, _ = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    (entry,) = bridge.orders.values()
    payload = bridge.payload(entry)
    assert payload["targets"][0]["volume_lots"] == "0.003"
    assert Decimal(payload["entry_price"]) == Decimal("60000.0")
    assert payload["note"] == entry.key
