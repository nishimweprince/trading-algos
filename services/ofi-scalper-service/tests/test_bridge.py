"""The execution bridge: shadow fills and halts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ofi_scalper_service.alerts import Alerts
from ofi_scalper_service.execution_bridge import DEFAULT_FILTERS, ExecutionBridge
from ofi_scalper_service.policy import Phase
from ofi_scalper_service.risk import HaltReason, RiskLimits, RiskState
from tests.conftest import RecordingNotifier, make_settings
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
    assert bridge.reject_backoff == {}  # an expired post-only is not a refusal


def test_shadow_entry_timeout_does_not_back_off(tmp_path: Path) -> None:
    dial = Dial()
    bridge, _ = shadow_bridge(tmp_path, dial)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    step = 100_000_000
    t = T0
    while bridge.counts.get("cycles_unfilled", 0) == 0:  # rests until its timeout
        t += step
        bridge.on_sample(sample(t), GATE_ON)
        assert t - T0 < 10 * S
    assert bridge.reject_backoff == {}
    bridge.on_sample(sample(t + step), GATE_ON)
    assert bridge.policies["BTCUSDT"].phase is Phase.ENTRY_RESTING  # enters again at once


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


def test_shadow_time_stop_exits_as_a_maker_and_the_guard_cancels_entries(tmp_path: Path) -> None:
    from tests.model_fixture import POLICY

    dial = Dial()
    settings = make_settings(
        tmp_path, OFI_EXECUTION_MODE="shadow", OFI_MODEL_VERSION="v1", OFI_ORDER_NOTIONAL_USD=180
    )
    policy = {**POLICY, "exit_mode": "maker_chase", "exit_chase_ms": 5000, "guard_z": 3.0}
    bridge = ExecutionBridge(
        settings,
        risk=RiskState(RiskLimits.from_settings(settings)),
        alerts=Alerts(RecordingNotifier()),
        model=dial_model(tmp_path / "models", dial, policy=policy),
        filters=dict(DEFAULT_FILTERS),
        fees=lambda _s: FEES,
    )
    assert bridge.flow_guard is not None and bridge.flow_guard.enabled
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    bridge.on_trade("BTCUSDT", T0 + 2, 59999.9)
    btc = bridge.policies["BTCUSDT"]
    assert btc.phase is Phase.OPEN
    dial.set()
    bridge.on_sample(sample(T0 + 10 * S + 2, bid=60001.0, ask=60001.1), GATE_ON)
    exit1 = bridge.orders[f"{btc.cycle.cycle_id}|exit1"]
    assert exit1.post_only and exit1.reduce_only and exit1.price == "60001.1"
    bridge.on_trade("BTCUSDT", T0 + 11 * S, 60001.2)  # bought through our ask
    assert btc.phase is Phase.FLAT
    bridge.flush_pending()
    (trade,) = bridge.book.recent_trades
    assert trade["exit_reason"] == "time_stop" and trade["legs"]["exit1"]["liquidity"] == "maker"
    assert trade["exit_price"] == 60001.1
    # Fees: maker on both legs, no taker.
    assert trade["fees_usd"] == pytest.approx(0.003 * (60000 + 60001.1) * 2 / 10_000, abs=1e-6)

    # The guard: after warming, a burst of selling flow cancels a resting bid.
    t = T0 + 20 * S
    for i in range(3100):
        t += 100_000_000
        bridge.on_sample({**sample(t), "ofi_int_1000ms": 1.0 if i % 2 else -1.0}, GATE_ON)
    dial.set(up=0.8, down=0.05)
    t += 100_000_000
    bridge.on_sample(sample(t), GATE_ON)
    assert btc.phase is Phase.ENTRY_RESTING
    bridge.on_sample({**sample(t + 100_000_000), "ofi_int_1000ms": -100.0}, GATE_ON)
    assert btc.phase is Phase.FLAT and bridge.flow_guard.flagged["buy"] == 1


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
