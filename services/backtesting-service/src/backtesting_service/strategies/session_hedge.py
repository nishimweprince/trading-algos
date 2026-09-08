"""session_hedge strategy execution: contingent-hedge staging behind the seam.

The pure triggers live in ``entry.hedge_pair``; the stateful staging moved here
verbatim from the engine so mode behaviour accumulates in the strategy module.
The engine keeps thin delegates (existing tests and call sites address the
engine) and calls through to these functions. Each function takes the engine
explicitly — carving a narrow facade so the strategy no longer reaches into
engine privates is the next increment, not this one.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..engine_types import EntryLot, EntryOrder, Pair, bar_open
from ..entry import (
    contingent_hedge_fill,
    contingent_hedge_touched,
    hedge_pair_plan,
    synthetic_order_plan,
)
from ..models import Candle, EngineEvent, EntryMode, IntrabarMode, OcoBufferMode
from .facade import StrategyEngine

if TYPE_CHECKING:
    from ..harness.fills import OcoTriggerHit


session_driven = True


def on_bar(engine: StrategyEngine, bar: Candle) -> None:
    """Session-driven staging happens at anchors, not per bar — no-op hook."""
    del engine, bar
    return None


def stage_fractional_contingent(
    engine: StrategyEngine,
    session: str,
    entry: float,
    range_price: float,
    ts: datetime,
    bullish: bool,
) -> bool:
    """Stage a fractional contingent-hedge pair plus its synthetic trigger order."""
    if engine._filter_blocks(session, range_price, ts, bullish):
        return False
    sl_dist = engine._sized_stop(range_price, session, ts)
    if sl_dist is None:
        return False
    decision = engine._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
    if decision is None:
        return False
    ratio = engine.params.hedge_ratio_initial
    initial_qty = decision.qty * ratio
    plan = hedge_pair_plan(
        entry=entry, sl_dist=sl_dist, rr=engine.params.rr, tp_r=engine._initial_target_r()
    )
    pair = Pair(
        id=f"{session}:{ts.isoformat()}",
        session=session,
        entry=entry,
        reference_entry=entry,
        sl_dist=sl_dist,
        long_sl=plan.long_sl,
        long_tp=plan.long_tp,
        short_sl=plan.short_sl,
        short_tp=plan.short_tp,
        qty=decision.qty,
        long_qty=initial_qty,
        short_qty=initial_qty,
        initial_risk_pct=decision.pair_risk_pct,
        initial_risk_cash=decision.pair_risk_cash,
        primary_side=None,
        entry_ts=ts,
        long_entry=entry,
        short_entry=entry,
        long_entry_ts=ts,
        short_entry_ts=ts,
        long_entry_lots=[EntryLot(ts, initial_qty)],
        short_entry_lots=[EntryLot(ts, initial_qty)],
        contingent_initial_ratio=ratio,
        hedge_ratio_staged=engine.params.hedge_ratio_staged,
        entry_mode=EntryMode.CONTINGENT_HEDGE,
        bullish_signal=bullish,
    )
    synthetic = synthetic_order_plan(
        entry=entry,
        sl_dist=sl_dist,
        rr=engine.params.rr,
        lock_dist=engine._plan_lock_offset(sl_dist),
        tp_r=engine._initial_target_r(),
    )
    order = EntryOrder(
        id=pair.id,
        session=session,
        mode=EntryMode.CONTINGENT_HEDGE,
        reference_entry=entry,
        sl_dist=sl_dist,
        upper_trigger=synthetic.upper_trigger,
        lower_trigger=synthetic.lower_trigger,
        bullish=bullish,
        staged_ts=ts,
        qty=decision.qty,
        initial_risk_pct=decision.pair_risk_pct,
        initial_risk_cash=decision.pair_risk_cash,
        long_sl=synthetic.long_sl,
        long_tp=synthetic.long_tp,
        short_sl=synthetic.short_sl,
        short_tp=synthetic.short_tp,
    )
    engine.pairs.append(pair)
    engine.entry_orders.append(order)
    engine.events.append(
        EngineEvent(
            kind="entry",
            session=session,
            ts=ts,
            detail={
                "entry": entry,
                "sl_dist": sl_dist,
                "bullish_signal": bullish,
                "primary_side": None,
                "pair_id": pair.id,
                "qty": initial_qty,
                "entry_mode": EntryMode.CONTINGENT_HEDGE.value,
                "hedge_ratio_initial": ratio,
            },
        )
    )
    return True


def scale_fractional_contingent(
    engine: StrategyEngine, pair: Pair, order: EntryOrder, hit: OcoTriggerHit, bar: Candle
) -> None:
    """Scale the surviving leg to full size once the contingent trigger fills."""
    assert hit.fill is not None and hit.side != "none"
    fill_ts = bar_open(bar, engine.params.timeframe_minutes)
    is_long = hit.side == "long"
    pair.primary_side = hit.side
    if is_long:
        engine._close_short(
            pair,
            hit.fill,
            fill_ts,
            reason="contingent_initial_stop",
            gap_fill=hit.gap,
        )
    else:
        engine._close_long(
            pair,
            hit.fill,
            fill_ts,
            reason="contingent_initial_stop",
            gap_fill=hit.gap,
        )
    current_qty = engine._leg_qty(pair, is_long)
    added_qty = max(0.0, pair.qty - current_qty)
    current_entry = engine._leg_entry(pair, is_long)
    average = (
        (current_entry * current_qty + hit.fill * added_qty) / pair.qty
        if pair.qty > 0
        else hit.fill
    )
    if is_long:
        pair.long_qty = pair.qty
        pair.long_entry = average
        pair.long_entry_fills += int(added_qty > 0)
        if added_qty > 0:
            pair.long_entry_lots.append(EntryLot(fill_ts, added_qty))
        pair.long_sl = order.long_sl
        pair.long_tp = order.long_tp
    else:
        pair.short_qty = pair.qty
        pair.short_entry = average
        pair.short_entry_fills += int(added_qty > 0)
        if added_qty > 0:
            pair.short_entry_lots.append(EntryLot(fill_ts, added_qty))
        pair.short_sl = order.short_sl
        pair.short_tp = order.short_tp
    pair.locked = True
    pair.entry_gap = hit.gap
    pair.entry_ambiguous = hit.ambiguous
    pair.entry_bar_close_ts = bar.ts
    pair.entry_m1_index = hit.child_index
    pair.hedge_failure_threshold = engine._failure_threshold(
        order.reference_entry, order.sl_dist, is_long
    )
    engine.events.append(
        EngineEvent(
            kind="entry",
            session=pair.session,
            ts=fill_ts,
            detail={
                "entry": hit.fill,
                "reference_entry": order.reference_entry,
                "pair_id": pair.id,
                "primary_side": pair.primary_side,
                "qty": added_qty,
                "entry_mode": EntryMode.CONTINGENT_HEDGE.value,
                "hedge_ratio_initial": pair.contingent_initial_ratio,
                "gap_fill": hit.gap,
            },
        )
    )


def stage_contingent_hedges(engine: StrategyEngine, bar: Candle) -> None:
    """Stage the hedge leg wherever the failure trigger was touched."""
    fill_ts = bar_open(bar, engine.params.timeframe_minutes)
    for pair in engine.pairs:
        threshold = pair.hedge_failure_threshold
        if (
            pair.contingent_initial_ratio is None
            or pair.primary_side is None
            or pair.hedge_staged
            or threshold is None
            or pair.hedge_ratio_staged <= 0
        ):
            continue
        if (
            pair.entry_bar_close_ts == bar.ts
            and engine.params.intrabar_mode is IntrabarMode.OPTIMISTIC
        ):
            continue
        long_primary = pair.primary_side == "long"
        touched = contingent_hedge_touched(
            threshold=threshold, bar_low=bar.low, bar_high=bar.high, long_primary=long_primary
        )
        if not touched:
            continue
        desired_qty = pair.qty * pair.hedge_ratio_staged
        hedge_is_long = not long_primary
        hedge_open = pair.long_open if hedge_is_long else pair.short_open
        current_qty = engine._leg_qty(pair, hedge_is_long) if hedge_open else 0.0
        added_qty = max(0.0, desired_qty - current_qty)
        if added_qty <= 0:
            pair.hedge_staged = True
            continue
        if long_primary:
            fill = contingent_hedge_fill(bar_open=bar.open, threshold=threshold, long_primary=True)
            pair.short_open = True
            pair.short_episode += int(not hedge_open)
            pair.short_entry = fill
            pair.short_entry_ts = fill_ts
            pair.short_qty = desired_qty
            pair.short_entry_fills = 1
            pair.short_entry_lots = [EntryLot(fill_ts, desired_qty)]
        else:
            fill = contingent_hedge_fill(bar_open=bar.open, threshold=threshold, long_primary=False)
            pair.long_open = True
            pair.long_episode += int(not hedge_open)
            pair.long_entry = fill
            pair.long_entry_ts = fill_ts
            pair.long_qty = desired_qty
            pair.long_entry_fills = 1
            pair.long_entry_lots = [EntryLot(fill_ts, desired_qty)]
        pair.hedge_staged = True
        engine.events.append(
            EngineEvent(
                kind="hedge_staged",
                session=pair.session,
                ts=fill_ts,
                detail={
                    "pair_id": pair.id,
                    "side": "long" if hedge_is_long else "short",
                    "fill": fill,
                    "failure_threshold": threshold,
                    "qty": added_qty,
                    "hedge_ratio_staged": pair.hedge_ratio_staged,
                },
            )
        )


def stage_oco_bracket(
    engine: StrategyEngine,
    *,
    session: str,
    entry: float,
    range_price: float,
    range_high: float,
    range_low: float,
    ts: datetime,
    bullish: bool,
) -> bool:
    if engine._filter_blocks(session, range_price, ts, bullish):
        return False
    sl_dist = engine._sized_stop(range_price, session, ts)
    if sl_dist is None:
        return False
    decision = engine._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
    if decision is None:
        return False
    buffer_price = (
        engine.params.oco_buffer_value * range_price
        if engine.params.oco_buffer_mode is OcoBufferMode.ORB_FRAC
        else engine.params.oco_buffer_value * engine.params.pip_size
    )
    order = EntryOrder(
        id=f"{session}:{ts.isoformat()}",
        session=session,
        mode=EntryMode.OCO_BRACKET,
        reference_entry=entry,
        sl_dist=sl_dist,
        upper_trigger=range_high + buffer_price,
        lower_trigger=range_low - buffer_price,
        bullish=bullish,
        staged_ts=ts,
        qty=decision.qty,
        initial_risk_pct=decision.pair_risk_pct,
        initial_risk_cash=decision.pair_risk_cash,
        long_sl=0.0,
        long_tp=0.0,
        short_sl=0.0,
        short_tp=0.0,
        expiry_bars=engine.params.oco_expiry_bars,
        root_id=f"{session}:{ts.isoformat()}",
    )
    engine.entry_orders.append(order)
    engine.events.append(
        EngineEvent(
            kind="entry_order_staged",
            session=session,
            ts=ts,
            detail={
                "entry_mode": EntryMode.OCO_BRACKET.value,
                "pair_id": order.id,
                "upper_trigger": order.upper_trigger,
                "lower_trigger": order.lower_trigger,
                "buffer": buffer_price,
                "expiry_bars": order.expiry_bars,
                "reentry_index": 0,
                "qty": order.qty,
                "sl_dist": order.sl_dist,
                "target_r": engine._initial_target_r(),
            },
        )
    )
    return True


def stage_synthetic_order(
    engine: StrategyEngine,
    session: str,
    entry: float,
    range_price: float,
    ts: datetime,
    bullish: bool,
) -> bool:
    if engine._filter_blocks(session, range_price, ts, bullish):
        return False
    sl_dist = engine._sized_stop(range_price, session, ts)
    if sl_dist is None:
        return False
    decision = engine._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
    if decision is None:
        return False
    plan = synthetic_order_plan(
        entry=entry,
        sl_dist=sl_dist,
        rr=engine.params.rr,
        lock_dist=engine._plan_lock_offset(sl_dist),
        tp_r=engine._initial_target_r(),
    )
    order = EntryOrder(
        id=f"{session}:{ts.isoformat()}",
        session=session,
        mode=engine.params.entry_mode,
        reference_entry=entry,
        sl_dist=sl_dist,
        upper_trigger=plan.upper_trigger,
        lower_trigger=plan.lower_trigger,
        bullish=bullish,
        staged_ts=ts,
        qty=decision.qty,
        initial_risk_pct=decision.pair_risk_pct,
        initial_risk_cash=decision.pair_risk_cash,
        long_sl=plan.long_sl,
        long_tp=plan.long_tp,
        short_sl=plan.short_sl,
        short_tp=plan.short_tp,
    )
    engine.entry_orders.append(order)
    engine.events.append(
        EngineEvent(
            kind="entry_order_staged",
            session=session,
            ts=ts,
            detail={
                "entry_mode": engine.params.entry_mode.value,
                "pair_id": order.id,
                "reference_entry": entry,
                "upper_trigger": plan.upper_trigger,
                "lower_trigger": plan.lower_trigger,
                "sl_dist": sl_dist,
                "qty": decision.qty,
            },
        )
    )
    return True


def stage_oco_reentries(engine: StrategyEngine, bar: Candle) -> None:
    if not engine.params.allow_reentry:
        return
    for pair in engine.pairs:
        if (
            pair.entry_mode is not EntryMode.OCO_BRACKET
            or pair.long_open
            or pair.short_open
            or pair.reentry_index != 0
            or pair.reentry_staged
            or pair.bracket_upper is None
            or pair.bracket_lower is None
        ):
            continue
        pair.reentry_staged = True
        reference = pair.reference_entry if pair.reference_entry is not None else pair.entry
        decision = engine._accept_structure(
            session=pair.session,
            entry=reference,
            sl_dist=pair.sl_dist,
            ts=bar.ts,
        )
        if decision is None:
            continue
        root_id = pair.root_id or pair.id
        order = EntryOrder(
            id=f"{root_id}:reentry:1",
            session=pair.session,
            mode=EntryMode.OCO_BRACKET,
            reference_entry=reference,
            sl_dist=pair.sl_dist,
            upper_trigger=pair.bracket_upper,
            lower_trigger=pair.bracket_lower,
            bullish=pair.bullish_signal,
            staged_ts=bar.ts,
            qty=decision.qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            long_sl=0.0,
            long_tp=0.0,
            short_sl=0.0,
            short_tp=0.0,
            expiry_bars=engine.params.oco_expiry_bars,
            reentry_index=1,
            root_id=root_id,
        )
        engine.entry_orders.append(order)
        engine.events.append(
            EngineEvent(
                kind="entry_order_staged",
                session=pair.session,
                ts=bar.ts,
                detail={
                    "entry_mode": EntryMode.OCO_BRACKET.value,
                    "pair_id": order.id,
                    "upper_trigger": order.upper_trigger,
                    "lower_trigger": order.lower_trigger,
                    "expiry_bars": order.expiry_bars,
                    "reentry_index": 1,
                    "qty": order.qty,
                    "sl_dist": order.sl_dist,
                    "target_r": engine._initial_target_r(),
                },
            )
        )


def open_pair(
    engine: StrategyEngine,
    session: str,
    entry: float,
    range_price: float,
    ts: datetime,
    bullish: bool,
) -> bool:
    if engine._filter_blocks(session, range_price, ts, bullish):
        return False
    sl_dist = engine._sized_stop(range_price, session, ts)
    if sl_dist is None:
        return False
    plan = hedge_pair_plan(
        entry=entry, sl_dist=sl_dist, rr=engine.params.rr, tp_r=engine._initial_target_r()
    )
    decision = engine._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
    if decision is None:
        return False
    pair = Pair(
        id=f"{session}:{ts.isoformat()}",
        session=session,
        entry=plan.reference_entry,
        sl_dist=plan.sl_dist,
        long_sl=plan.long_sl,
        long_tp=plan.long_tp,
        short_sl=plan.short_sl,
        short_tp=plan.short_tp,
        qty=decision.qty,
        long_qty=decision.qty,
        short_qty=decision.qty,
        initial_risk_pct=decision.pair_risk_pct,
        initial_risk_cash=decision.pair_risk_cash,
        primary_side="long" if bullish else "short",
        entry_ts=ts,
        long_open=plan.long_open,
        short_open=plan.short_open,
        long_entry=plan.long_entry,
        short_entry=plan.short_entry,
        long_entry_ts=ts,
        short_entry_ts=ts,
        long_entry_lots=[EntryLot(ts, decision.qty)],
        short_entry_lots=[EntryLot(ts, decision.qty)],
        entry_mode=engine.params.entry_mode,
        bullish_signal=bullish,
    )
    engine.pairs.append(pair)
    emit_entry(engine, pair, ts, bullish_signal=bullish)
    return True


def emit_entry(engine: StrategyEngine, pair: Pair, ts: datetime, *, bullish_signal: bool) -> None:
    engine.events.append(
        EngineEvent(
            kind="entry",
            session=pair.session,
            ts=ts,
            detail={
                "entry": pair.entry,
                "sl_dist": pair.sl_dist,
                "sl_pips": pair.sl_dist / engine.params.pip_size,
                "bullish_signal": bullish_signal,
                "primary_side": pair.primary_side,
                "pair_id": pair.id,
                "qty": pair.qty,
                "initial_risk_pct": pair.initial_risk_pct,
                "initial_risk_cash": pair.initial_risk_cash,
            },
        )
    )
