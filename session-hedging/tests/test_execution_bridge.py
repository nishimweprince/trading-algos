"""Engine events to broker orders: submit, cancel, amend, halt, and restart safety."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candles import CandleStore
from config import Settings
from engine import ClosedBarEngine
from execution import ExecutionResult, ExecutionState
from execution_bridge import ExecutionBridge
from models import Candle, EngineEvent, EntryMode
from notifier import Notifier
from paper import PaperTrader
from sessions import build_windows

# Current rather than fixed: the bridge refuses bars stale enough that their bracket
# would already have expired, so a hard-coded past timestamp would exercise that guard
# instead of the normal path.
BAR = Candle(
    ts=datetime.now(tz=UTC),
    open=2000,
    high=2010,
    low=1999,
    close=2008,
    volume=1.0,
    provider="test",
    source_instrument="XAUUSD",
)


class FakeClient:
    """Records calls and replays queued results, standing in for the gateway."""

    def __init__(self, account: str = "forex_demo") -> None:
        self.account = account
        self.source = "session_hedging"
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[int] = []
        self.amended: list[tuple[int, float | None]] = []
        self.lookups: list[str] = []
        self.submit_result = ExecutionResult(
            ExecutionState.SUCCEEDED,
            response={"targets": [{"account": "forex_demo", "order_id": 71}]},
        )
        self.cancel_result = ExecutionResult(ExecutionState.SUCCEEDED, response={"targets": []})
        self.lookup_result = ExecutionResult(ExecutionState.NOT_FOUND)

    def build_stop_entry(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "operation_id": str(kwargs["operation_id"]),
            "execution_type": "stop",
            "direction": kwargs["direction"],
            "entry_price": kwargs["entry_price"],
            "stop_loss_distance": kwargs["stop_distance"],
            "take_profit_distance": kwargs["target_distance"],
            "expires_at": kwargs["expires_at"],
        }

    async def submit(self, payload: dict[str, Any]) -> ExecutionResult:
        self.submitted.append(payload)
        return self.submit_result

    async def cancel_order(self, *, order_id: int, **_kw: Any) -> ExecutionResult:
        self.cancelled.append(order_id)
        return self.cancel_result

    async def amend_protection(
        self, *, position_id: int, stop_loss: float | None = None, **_kw: Any
    ) -> ExecutionResult:
        self.amended.append((position_id, stop_loss))
        return ExecutionResult(ExecutionState.SUCCEEDED, response={"targets": []})

    async def get_operation(self, operation_id: Any) -> ExecutionResult:
        self.lookups.append(str(operation_id))
        return self.lookup_result


def _settings(tmp_path: Path, mode: str = "live", **over: Any) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        market_execution_mode=mode,
        execution_account="forex_demo",
        ctrader_api_key="0123456789abcdef0123",
        **over,
    )


def _bridge(tmp_path: Path, mode: str = "live", **over: Any) -> tuple[ExecutionBridge, FakeClient]:
    client = FakeClient()
    return ExecutionBridge(_settings(tmp_path, mode, **over), client), client  # type: ignore[arg-type]


def _staged(pair_id: str = "new_york:2026-01-14T14:00:00+00:00") -> EngineEvent:
    return EngineEvent(
        kind="entry_order_staged",
        session="new_york",
        ts=BAR.ts,
        detail={
            "entry_mode": EntryMode.OCO_BRACKET.value,
            "pair_id": pair_id,
            "upper_trigger": 2011.0,
            "lower_trigger": 1998.0,
            "sl_dist": 22.0,
            "target_r": 3.0,
            "expiry_bars": 1,
            "qty": 1.0,
        },
    )


class TestStaging:
    @pytest.mark.asyncio
    async def test_stages_both_sides_of_the_bracket(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        assert len(client.submitted) == 2
        directions = {payload["direction"] for payload in client.submitted}
        assert directions == {"buy", "sell"}

    @pytest.mark.asyncio
    async def test_triggers_and_distances_come_from_the_event(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        buy = next(p for p in client.submitted if p["direction"] == "buy")
        sell = next(p for p in client.submitted if p["direction"] == "sell")
        assert buy["entry_price"] == 2011.0
        assert sell["entry_price"] == 1998.0
        assert buy["stop_loss_distance"] == 22.0
        assert buy["take_profit_distance"] == 66.0, "sl_dist * target_r"

    @pytest.mark.asyncio
    async def test_gtd_expiry_outlives_the_engine_cancel(self, tmp_path: Path) -> None:
        """One bar of grace, so the engine's own cancel normally lands first."""
        bridge, client = _bridge(tmp_path, timeframe="H1")
        await bridge.handle(_staged(), BAR)
        expires = client.submitted[0]["expires_at"]
        assert expires is not None
        assert (expires - BAR.ts).total_seconds() == 2 * 3600

    @pytest.mark.asyncio
    async def test_records_the_broker_order_id_for_later_cancel(self, tmp_path: Path) -> None:
        bridge, _client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        assert all(order.order_id == 71 for order in bridge.tracked())

    @pytest.mark.asyncio
    async def test_ignores_modes_that_do_not_rest_orders(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        event = _staged()
        event.detail["entry_mode"] = EntryMode.HEDGE_PAIR.value
        await bridge.handle(event, BAR)
        assert client.submitted == []

    @pytest.mark.asyncio
    async def test_incomplete_detail_submits_nothing(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        event = _staged()
        del event.detail["sl_dist"]
        await bridge.handle(event, BAR)
        assert client.submitted == []


class TestShadowMode:
    @pytest.mark.asyncio
    async def test_builds_payloads_but_sends_nothing(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path, mode="shadow")
        await bridge.handle(_staged(), BAR)
        assert client.submitted == [], "shadow must not reach the broker"
        assert len(bridge.tracked()) == 2
        assert all(order.shadow and order.payload for order in bridge.tracked())
        assert all(order.state == "shadow" for order in bridge.tracked())


class TestCancellation:
    @pytest.mark.asyncio
    async def test_sibling_cancel_kills_only_the_named_side(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        await bridge.handle(
            EngineEvent(
                kind="entry_order_cancelled",
                session="new_york",
                ts=BAR.ts,
                detail={
                    "pair_id": _staged().detail["pair_id"],
                    "reason": "oco_sibling",
                    "cancelled_side": "short",
                },
            ),
            BAR,
        )
        assert client.cancelled == [71]
        legs = bridge.orders[str(_staged().detail["pair_id"])]
        assert legs["short"].state == "cancelled"
        assert legs["long"].state != "cancelled", "the filled side must survive"

    @pytest.mark.asyncio
    async def test_expiry_cancels_both_sides(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        await bridge.handle(
            EngineEvent(
                kind="entry_order_cancelled",
                session="new_york",
                ts=BAR.ts,
                detail={"pair_id": _staged().detail["pair_id"], "reason": "expired"},
            ),
            BAR,
        )
        assert len(client.cancelled) == 2

    @pytest.mark.asyncio
    async def test_already_expired_order_is_not_an_error(self, tmp_path: Path) -> None:
        """A GTD order the broker already dropped returns order_not_found. That is the
        intended end state, not a failure to count against the halt threshold."""
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        client.cancel_result = ExecutionResult(ExecutionState.REJECTED, reason="order_not_found")
        await bridge.handle(
            EngineEvent(
                kind="entry_order_cancelled",
                session="new_york",
                ts=BAR.ts,
                detail={"pair_id": _staged().detail["pair_id"], "reason": "expired"},
            ),
            BAR,
        )
        assert bridge.halted_reason is None
        assert all(order.reason == "already_gone" for order in bridge.tracked())


class TestFillAndAmend:
    @pytest.mark.asyncio
    async def test_entry_captures_the_position_id(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        client.lookup_result = ExecutionResult(
            ExecutionState.SUCCEEDED,
            response={
                "targets": [
                    {
                        "account": "forex_demo",
                        "order_id": 71,
                        "position_id": 91,
                        "execution_price": "2011.4",
                    }
                ]
            },
        )
        await bridge.handle(
            EngineEvent(
                kind="entry",
                session="new_york",
                ts=BAR.ts,
                detail={"pair_id": _staged().detail["pair_id"], "primary_side": "long"},
            ),
            BAR,
        )
        order = bridge.orders[str(_staged().detail["pair_id"])]["long"]
        assert order.position_id == 91
        assert order.fill_price == pytest.approx(2011.4)

    @pytest.mark.asyncio
    async def test_ratchet_moves_the_stop_on_the_open_position(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        bridge.orders[str(_staged().detail["pair_id"])]["long"].position_id = 91
        await bridge.handle(
            EngineEvent(
                kind="be_ratchet_armed",
                session="new_york",
                ts=BAR.ts,
                detail={
                    "pair_id": _staged().detail["pair_id"],
                    "side": "long",
                    "new_sl": 2013.0,
                },
            ),
            BAR,
        )
        assert client.amended == [(91, 2013.0)]

    @pytest.mark.asyncio
    async def test_no_amend_before_a_position_exists(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        await bridge.handle(
            EngineEvent(
                kind="be_ratchet_armed",
                session="new_york",
                ts=BAR.ts,
                detail={"pair_id": _staged().detail["pair_id"], "side": "long", "new_sl": 2013.0},
            ),
            BAR,
        )
        assert client.amended == []


class TestHalting:
    @pytest.mark.asyncio
    async def test_repeated_gateway_failures_halt_the_bridge(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path, execution_max_consecutive_failures=2)
        client.submit_result = ExecutionResult(ExecutionState.UNKNOWN, reason="ConnectError")
        await bridge.handle(_staged(), BAR)
        assert bridge.halted_reason is not None
        assert "consecutive" in bridge.halted_reason

    @pytest.mark.asyncio
    async def test_a_halted_bridge_ignores_further_events(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        await bridge.halt("manual")
        await bridge.handle(_staged(), BAR)
        assert client.submitted == []

    @pytest.mark.asyncio
    async def test_prop_guard_breach_cancels_resting_orders(self, tmp_path: Path) -> None:
        """The engine's guard only blocks new structures; orders already at the broker
        are untouched by it, so the bridge must pull them itself."""
        bridge, client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        await bridge.handle(
            EngineEvent(
                kind="prop_guard_breached",
                session="risk",
                ts=BAR.ts,
                detail={"reason": "daily_loss_limit", "equity_cash": 1.0},
            ),
            BAR,
        )
        assert len(client.cancelled) == 2
        assert bridge.halted_reason is not None

    @pytest.mark.asyncio
    async def test_a_success_resets_the_failure_count(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path, execution_max_consecutive_failures=3)
        client.submit_result = ExecutionResult(ExecutionState.UNKNOWN, reason="ConnectError")
        await bridge.handle(_staged("a"), BAR)
        assert bridge.consecutive_failures == 2
        client.submit_result = ExecutionResult(
            ExecutionState.SUCCEEDED,
            response={"targets": [{"account": "forex_demo", "order_id": 72}]},
        )
        await bridge.handle(_staged("b"), BAR)
        assert bridge.consecutive_failures == 0


class TestRestartSafety:
    @pytest.mark.asyncio
    async def test_state_round_trips_through_the_snapshot(self, tmp_path: Path) -> None:
        bridge, _client = _bridge(tmp_path)
        await bridge.handle(_staged(), BAR)
        restored, _ = _bridge(tmp_path)
        restored.restore(bridge.snapshot())
        assert {o.operation_id for o in restored.tracked()} == {
            o.operation_id for o in bridge.tracked()
        }
        assert all(order.order_id == 71 for order in restored.tracked())

    @pytest.mark.asyncio
    async def test_reconcile_asks_before_assuming(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path)
        client.submit_result = ExecutionResult(ExecutionState.PENDING, response={"targets": []})
        await bridge.handle(_staged(), BAR)
        restored, restored_client = _bridge(tmp_path)
        restored.restore(bridge.snapshot())
        await restored.reconcile()
        assert len(restored_client.lookups) == 2, "pending operations must be resolved, not resent"
        assert restored_client.submitted == [], "reconcile must never submit"

    @pytest.mark.asyncio
    async def test_paper_restart_does_not_resubmit(self, tmp_path: Path) -> None:
        """The order-tracking equivalent of test_paper_reload_skips_duplicate."""
        settings = _settings(tmp_path)
        first_client = FakeClient()
        first_bridge = ExecutionBridge(settings, first_client)  # type: ignore[arg-type]
        engine = ClosedBarEngine(build_windows(["new_york"], {}), settings.engine_params())
        trader = PaperTrader(
            settings,
            CandleStore(settings, client=None),  # type: ignore[arg-type]
            engine,
            Notifier(settings, client=None),  # type: ignore[arg-type]
            settings.paper_state_path,
            bridge=first_bridge,
        )
        await first_bridge.handle(_staged(), BAR)
        trader.save()

        second_client = FakeClient()
        second_bridge = ExecutionBridge(settings, second_client)  # type: ignore[arg-type]
        restored = PaperTrader(
            settings,
            CandleStore(settings, client=None),  # type: ignore[arg-type]
            ClosedBarEngine(build_windows(["new_york"], {}), settings.engine_params()),
            Notifier(settings, client=None),  # type: ignore[arg-type]
            settings.paper_state_path,
            bridge=second_bridge,
        )
        restored.load()
        assert len(second_bridge.tracked()) == 2
        assert second_client.submitted == [], "a restart must not re-place resting orders"

    def test_state_file_is_written_atomically(self, tmp_path: Path) -> None:
        """A crash mid-write must not lose the order ids needed to cancel."""
        settings = _settings(tmp_path)
        engine = ClosedBarEngine(build_windows(["new_york"], {}), settings.engine_params())
        trader = PaperTrader(
            settings,
            CandleStore(settings, client=None),  # type: ignore[arg-type]
            engine,
            Notifier(settings, client=None),  # type: ignore[arg-type]
            settings.paper_state_path,
        )
        trader.save()
        assert settings.paper_state_path.is_file()
        leftovers = list(settings.paper_state_path.parent.glob("*.tmp"))
        assert leftovers == [], "the temp file must be renamed, not left behind"


class TestStaleBars:
    @pytest.mark.asyncio
    async def test_a_bar_older_than_its_own_expiry_is_refused(self, tmp_path: Path) -> None:
        """Stale bar means stale trigger levels; the price has already moved past them."""
        bridge, client = _bridge(tmp_path, timeframe="H1")
        stale = Candle(
            ts=datetime(2020, 1, 1, 14, 0, tzinfo=UTC),
            open=2000,
            high=2010,
            low=1999,
            close=2008,
            volume=1.0,
            provider="test",
            source_instrument="XAUUSD",
        )
        await bridge.handle(_staged(), stale)
        assert client.submitted == []
        assert bridge.tracked() == []

    @pytest.mark.asyncio
    async def test_a_current_bar_is_accepted(self, tmp_path: Path) -> None:
        bridge, client = _bridge(tmp_path, timeframe="H1")
        fresh = Candle(
            ts=datetime.now(tz=UTC),
            open=2000,
            high=2010,
            low=1999,
            close=2008,
            volume=1.0,
            provider="test",
            source_instrument="XAUUSD",
        )
        await bridge.handle(_staged(), fresh)
        assert len(client.submitted) == 2


class TestLiveViewState:
    def test_equity_curve_survives_a_restart(self, tmp_path: Path) -> None:
        """The live page's headline chart is built from this; losing it on restart would
        leave the chart permanently empty for the rest of the run."""
        settings = _settings(tmp_path)
        engine = ClosedBarEngine(
            build_windows(["new_york"], {}),
            settings.engine_params(),
            collect_equity_curve=True,
        )
        engine._equity_curve_pips[BAR.ts] = (123.5, -20.0)

        restored = ClosedBarEngine(
            build_windows(["new_york"], {}),
            settings.engine_params(),
            collect_equity_curve=True,
        )
        restored.restore(engine.snapshot())
        points = restored.equity_curve_points()
        assert len(points) == 1
        assert points[0].net_equity == pytest.approx(123.5)
        assert points[0].net_drawdown == pytest.approx(-20.0)
