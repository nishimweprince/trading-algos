"""The strategy facade contract: the engine satisfies it structurally.

If a facade member is renamed or removed on either side, this fails before
the strategy module can drift from the engine it runs against.
"""

from __future__ import annotations

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.sessions import build_windows


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
