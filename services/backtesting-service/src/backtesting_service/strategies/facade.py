"""Narrow engine facade: the exact surface strategy modules may use.

``ClosedBarEngine`` satisfies this ``Protocol`` structurally; strategies name
the facade, never the engine, so the dependency points one way (engine →
strategies) and a strategy can be exercised against a fake. Member names mirror
the engine's privates verbatim — a future rename pass can drop the underscores,
but this module documents the true surface without churning the engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..engine_types import EntryOrder, Pair
from ..harness.sizing import SizingDecision
from ..models import EngineEvent, EngineParams


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
