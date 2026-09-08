"""The strategy facade contract: the engine satisfies it structurally.

If a facade member is renamed or removed on either side, this fails before
the strategy module can drift from the engine it runs against. The second half
pins the other direction -- that the engine dispatches staging through the
strategy it was bound to rather than through an import.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import Candle
from backtesting_service.sessions import build_windows
from backtesting_service.strategies import session_hedge


def test_engine_satisfies_strategy_facade() -> None:
    engine = ClosedBarEngine(build_windows(["new_york"], {}), Settings().engine_params())
    for name in (
        "_filter_blocks",
        "_sized_stop",
        "_accept_structure",
        "_close_long",
        "_close_short",
        "_leg_qty",
        "_leg_entry",
        "_plan_lock_offset",
        "_initial_target_r",
        "_failure_threshold",
    ):
        assert callable(getattr(engine, name)), name
    for name in ("params", "pairs", "entry_orders", "events"):
        assert hasattr(engine, name), name


def test_engine_binds_the_builtin_staging_by_default() -> None:
    """Every existing call site -- research, comparison, the determinism gate."""
    engine = ClosedBarEngine(build_windows(["new_york"], {}), Settings().engine_params())
    assert engine.strategy is session_hedge


def test_engine_calls_through_the_bound_strategy_not_the_builtin() -> None:
    """The seam: staging is dispatched, not imported by name.

    A recording double stands in for strategy number two; if the engine ever
    goes back to naming ``session_hedge`` directly, the delegate stops routing
    here and this fails.
    """
    calls: list[str] = []

    class Recorder:
        def __getattr__(self, name: str):
            def record(*args: object, **kwargs: object) -> bool:
                calls.append(name)
                return False

            return record

    engine = ClosedBarEngine(
        build_windows(["new_york"], {}), Settings().engine_params(), strategy=Recorder()
    )
    ts = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
    bar = Candle(
        ts=ts,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )
    engine._open_pair("new_york", 1.0, 1.0, ts, True)
    engine._stage_synthetic_order("new_york", 1.0, 1.0, ts, True)
    engine._stage_fractional_contingent("new_york", 1.0, 1.0, ts, True)
    engine._stage_contingent_hedges(bar)
    engine._stage_oco_reentries(bar)
    engine._stage_oco_bracket(
        session="new_york",
        entry=1.0,
        range_price=1.0,
        range_high=2.0,
        range_low=0.5,
        ts=ts,
        bullish=True,
    )
    assert calls == [
        "open_pair",
        "stage_synthetic_order",
        "stage_fractional_contingent",
        "stage_contingent_hedges",
        "stage_oco_reentries",
        "stage_oco_bracket",
    ]
