"""The two halves of the strategy seam, as ``Protocol``s.

``StrategyEngine`` is the narrow engine facade: the exact surface strategy
modules may use. ``ClosedBarEngine`` satisfies it structurally; strategies name
the facade, never the engine, so the dependency points one way (engine →
strategies) and a strategy can be exercised against a fake. Member names mirror
the engine's privates verbatim — a future rename pass can drop the underscores,
but this module documents the true surface without churning the engine.

``StrategyExecution`` is the mirror: the surface the engine calls back out to.
Together they make the seam two-way-typed, so the engine binds to *a* strategy
rather than importing one by name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..engine_types import EntryOrder, Pair
from ..harness.fills import OcoTriggerHit
from ..harness.sizing import SizingDecision
from ..models import Candle, EngineEvent, EngineParams


class StrategyEngine(Protocol):
    """Mutable engine state plus the helpers strategy staging may call."""

    params: EngineParams
    pairs: list[Pair]
    entry_orders: list[EntryOrder]
    events: list[EngineEvent]

    def _filter_blocks(
        self, session: str, range_price: float, ts: datetime, bullish: bool
    ) -> bool: ...
    def _sized_stop(self, range_price: float, session: str, ts: datetime) -> float | None: ...
    def _accept_structure(
        self, *, session: str, entry: float, sl_dist: float, ts: datetime
    ) -> SizingDecision | None: ...
    def _close_long(
        self, pair: Pair, px: float, ts: datetime, reason: str = ..., *, gap_fill: bool = ...
    ) -> None: ...
    def _close_short(
        self, pair: Pair, px: float, ts: datetime, reason: str = ..., *, gap_fill: bool = ...
    ) -> None: ...
    def _leg_qty(self, pair: Pair, is_long: bool) -> float: ...
    def _leg_entry(self, pair: Pair, is_long: bool) -> float: ...
    def _plan_lock_offset(self, sl_dist: float) -> float: ...
    def _initial_target_r(self) -> float: ...
    def _failure_threshold(self, entry: float, sl_dist: float, is_long: bool) -> float: ...


class StrategyExecution(Protocol):
    """The staging surface the engine calls out to, one implementation per strategy.

    ``ClosedBarEngine`` holds one of these and calls through it, so registering
    strategy number two is an addition — a module satisfying this Protocol plus
    a ``registry`` entry — rather than an edit to the engine. Before this the
    engine imported ``strategies.session_hedge`` by name, which meant
    ``BacktestRequest.strategy`` chose whose ``build`` assembled the parameters
    but never whose code staged the entries.

    A *module* satisfies it, per PEP 544's module-as-implementation rule: the
    ``self`` parameters below match ``session_hedge``'s module-level functions,
    which take the engine explicitly.
    """

    def stage_synthetic_order(
        self,
        engine: StrategyEngine,
        session: str,
        entry: float,
        range_price: float,
        ts: datetime,
        bullish: bool,
    ) -> bool: ...
    def stage_oco_bracket(
        self,
        engine: StrategyEngine,
        *,
        session: str,
        entry: float,
        range_price: float,
        range_high: float,
        range_low: float,
        ts: datetime,
        bullish: bool,
    ) -> bool: ...
    def stage_fractional_contingent(
        self,
        engine: StrategyEngine,
        session: str,
        entry: float,
        range_price: float,
        ts: datetime,
        bullish: bool,
    ) -> bool: ...
    def scale_fractional_contingent(
        self,
        engine: StrategyEngine,
        pair: Pair,
        order: EntryOrder,
        hit: OcoTriggerHit,
        bar: Candle,
    ) -> None: ...
    def stage_contingent_hedges(self, engine: StrategyEngine, bar: Candle) -> None: ...
    def stage_oco_reentries(self, engine: StrategyEngine, bar: Candle) -> None: ...
    def open_pair(
        self,
        engine: StrategyEngine,
        session: str,
        entry: float,
        range_price: float,
        ts: datetime,
        bullish: bool,
    ) -> bool: ...
