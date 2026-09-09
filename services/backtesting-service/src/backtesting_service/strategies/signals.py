"""Shared staging helpers for signal-driven strategies (ipda, fu).

A signal-driven strategy implements ``on_bar``: once per closed bar it decides
a direction and protective levels, then stages a :class:`SignalOrder` through
:func:`stage`. The engine fills staged orders at the next bar open and owns
everything after the fill — stops, targets, ratchets, time exits, costs.
"""

from __future__ import annotations

from datetime import datetime

from ..engine_types import SignalOrder, bar_open
from ..models import Candle, EngineEvent
from .facade import StrategyEngine


def session_label(engine: StrategyEngine, open_ts: datetime, default: str) -> str | None:
    """Name of the session window containing ``open_ts``, or None if outside all."""
    for window in engine.windows:
        if window.contains(open_ts):
            return window.name
    return None


def bar_open_ts(engine: StrategyEngine, bar: Candle) -> datetime:
    return bar_open(bar, engine.params.timeframe_minutes)


def stage(
    engine: StrategyEngine,
    *,
    strategy: str,
    session: str,
    side: str,
    ref_entry: float,
    sl_price: float,
    tp_price: float,
    signal_ts: datetime,
    anchor_to_fill: bool,
    detail: dict[str, object] | None = None,
) -> bool:
    """Stage one market-fill intent for the next bar open. Deduplicated by id."""
    assert side in ("long", "short"), side
    merged: dict[str, object] = {"strategy": strategy}
    if detail:
        merged.update(detail)
    order = SignalOrder(
        id=f"{strategy}:{signal_ts.isoformat()}:{side}",
        strategy=strategy,
        session=session,
        side=side,  # type: ignore[arg-type]
        ref_entry=ref_entry,
        sl_price=sl_price,
        tp_price=tp_price,
        anchor_to_fill=anchor_to_fill,
        staged_ts=signal_ts,
        signal_ts=signal_ts,
        detail=merged,
    )
    return engine.stage_signal_order(order)


def skip_out_of_session(
    engine: StrategyEngine,
    *,
    strategy: str,
    session_names: str,
    ts: datetime,
    detail: dict[str, object] | None = None,
) -> None:
    """Record a signal the session gate suppressed. One event per call.

    Callers deduplicate per bucket (mirroring live, which notifies once per
    candle rather than once per poll) via ``engine.strategy_state``.
    """
    merged: dict[str, object] = {"strategy": strategy, "sessions": session_names}
    if detail:
        merged.update(detail)
    engine.events.append(
        EngineEvent(
            kind="signal_skipped_out_of_session",
            session=strategy,
            ts=ts,
            detail=merged,
        )
    )
