"""Four-mode comparison over one immutable candle set and shared configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from statistics import median
from typing import Literal

from anchors import SessionAnchor
from engine import ClosedBarEngine, Pair
from models import (
    BacktestReport,
    Candle,
    EngineParams,
    EntryMode,
    EntryModeComparisonReport,
    EntryModeComparisonRow,
    HedgeSyntheticAttribution,
    Timeframe,
    TradePairResult,
)
from sessions import SessionWindow

COMPARISON_MODES = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)


@dataclass(frozen=True)
class _ModeRun:
    row: EntryModeComparisonRow
    report: BacktestReport
    engine: ClosedBarEngine


def compare_entry_modes(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
) -> EntryModeComparisonReport:
    """Run all Phase 2 entry modes without mutating the candle or parameter inputs."""
    if not candles:
        raise ValueError("comparison requires at least one candle")

    runs: dict[EntryMode, _ModeRun] = {}
    for mode in COMPARISON_MODES:
        mode_params = EngineParams.model_validate(
            params.model_dump() | {"entry_mode": mode}
        )
        engine = ClosedBarEngine(windows, mode_params, anchors)
        engine.run(candles)
        report = engine.report(symbol, timeframe, source).model_copy(
            update={"bar_count": len(candles)}
        )
        runs[mode] = _ModeRun(
            row=_comparison_row(engine, report), report=report, engine=engine
        )

    shared_params = params.model_dump(mode="json")
    shared_params.pop("entry_mode", None)
    return EntryModeComparisonReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=_candle_sha256(candles),
        shared_params=shared_params,
        rows=[runs[mode].row for mode in COMPARISON_MODES],
        hedge_vs_synthetic=_attribution(
            runs[EntryMode.HEDGE_PAIR], runs[EntryMode.SYNTHETIC_BREAKOUT]
        ),
    )


def _comparison_row(
    engine: ClosedBarEngine, report: BacktestReport
) -> EntryModeComparisonRow:
    pairs = {pair.id: pair for pair in engine.pairs}
    completed = [result for result in report.trade_pairs if result.status == "closed"]
    gross_pips = [_value(result.gross_pnl_pips) for result in completed]
    net_pips = [_value(result.net_pnl_pips) for result in completed]
    gross_rs = [
        _pair_gross_r(result, pairs[result.id], engine.params) for result in completed
    ]
    net_rs = [
        gross_r - _pair_cost_r(result, pairs[result.id], engine.params)
        for gross_r, result in zip(gross_rs, completed, strict=True)
    ]
    holds = [_hold_hours(result) for result in completed]
    hold_values = [value for value in holds if value is not None]
    exit_fill_sides = len(engine.trades)
    cancelled = [event for event in report.events if event.kind == "entry_order_cancelled"]

    return EntryModeComparisonRow(
        entry_mode=report.entry_mode,
        completed_structures=len(completed),
        gross_pips=report.gross_equity_pips,
        net_pips=report.net_equity_pips,
        gross_r=report.gross_equity_r,
        net_r=report.net_equity_r,
        execution_cost_pips=report.execution_cost_pips,
        financing_cost_pips=report.financing_cost_pips,
        total_cost_pips=report.equity_cost_pips,
        gross_expectancy_pips=_mean(gross_pips),
        net_expectancy_pips=_mean(net_pips),
        gross_expectancy_r=_mean(gross_rs),
        net_expectancy_r=_mean(net_rs),
        gross_profit_factor=_profit_factor(gross_pips),
        net_profit_factor=_profit_factor(net_pips),
        gross_win_rate_excl_be=_win_rate_excl_be(gross_pips),
        net_win_rate_excl_be=_win_rate_excl_be(net_pips),
        survivor_tp_rate=report.survivor_tp_rate,
        breakeven_tp_rate_required=report.breakeven_tp_rate_required,
        gross_max_drawdown_pips=report.gross_max_drawdown_pips,
        net_max_drawdown_pips=report.net_max_drawdown_pips,
        gross_max_drawdown_r=report.gross_max_drawdown_r,
        net_max_drawdown_r=report.net_max_drawdown_r,
        breakeven_pips_per_completed_side=report.breakeven_pips_per_side,
        transaction_sides=report.transaction_sides,
        cost_side_equivalents=report.cost_side_equivalents,
        entry_fill_sides=report.transaction_sides - exit_fill_sides,
        exit_fill_sides=exit_fill_sides,
        cancelled_entry_orders=len(cancelled),
        expired_entry_orders=sum(
            event.detail.get("reason") == "expired" for event in cancelled
        ),
        median_hold_hours=float(median(hold_values)) if hold_values else None,
        p95_hold_hours=_percentile(hold_values, 0.95),
        max_concurrent_structures=report.max_concurrent_structures,
        suppressed_signals=report.suppressed_signal_count,
        unresolved_structures=report.unresolved_structures,
        prop_guard_breached=report.prop_guard_breached,
        prop_guard_breach_reason=report.prop_guard_breach_reason,
        prop_guard_breached_at=report.prop_guard_breached_at,
        prop_guard_breach_events=sum(
            event.kind == "prop_guard_breached" for event in report.events
        ),
    )


def _attribution(
    hedge: _ModeRun, synthetic: _ModeRun
) -> HedgeSyntheticAttribution:
    hedge_gap_pips, hedge_gap_r, hedge_gap_count = _tagged_gross(
        hedge, tag="gap"
    )
    synth_gap_pips, synth_gap_r, synth_gap_count = _tagged_gross(
        synthetic, tag="gap"
    )
    hedge_same_pips, hedge_same_r, hedge_same_count = _tagged_gross(
        hedge, tag="same_bar"
    )
    synth_same_pips, synth_same_r, synth_same_count = _tagged_gross(
        synthetic, tag="same_bar"
    )
    gross_pips = hedge.row.gross_pips - synthetic.row.gross_pips
    gross_r = hedge.row.gross_r - synthetic.row.gross_r
    gap_pips = hedge_gap_pips - synth_gap_pips
    gap_r = hedge_gap_r - synth_gap_r
    same_pips = hedge_same_pips - synth_same_pips
    same_r = hedge_same_r - synth_same_r
    payoff_pips = gross_pips - gap_pips - same_pips
    payoff_r = gross_r - gap_r - same_r
    execution_cost = hedge.row.execution_cost_pips - synthetic.row.execution_cost_pips
    financing_cost = hedge.row.financing_cost_pips - synthetic.row.financing_cost_pips
    total_cost = hedge.row.total_cost_pips - synthetic.row.total_cost_pips
    total_cost_r = (hedge.row.gross_r - hedge.row.net_r) - (
        synthetic.row.gross_r - synthetic.row.net_r
    )
    net_pips = hedge.row.net_pips - synthetic.row.net_pips
    net_r = hedge.row.net_r - synthetic.row.net_r

    return HedgeSyntheticAttribution(
        gross_difference_pips=gross_pips,
        gap_effect_pips=gap_pips,
        same_bar_effect_pips=same_pips,
        gross_payoff_effect_pips=payoff_pips,
        execution_cost_difference_pips=execution_cost,
        financing_cost_difference_pips=financing_cost,
        total_cost_difference_pips=total_cost,
        net_difference_pips=net_pips,
        reconciliation_error_pips=net_pips
        - (payoff_pips + gap_pips + same_pips - total_cost),
        gross_difference_r=gross_r,
        gap_effect_r=gap_r,
        same_bar_effect_r=same_r,
        gross_payoff_effect_r=payoff_r,
        total_cost_difference_r=total_cost_r,
        net_difference_r=net_r,
        reconciliation_error_r=net_r
        - (payoff_r + gap_r + same_r - total_cost_r),
        hedge_gap_tagged_structures=hedge_gap_count,
        synthetic_gap_tagged_structures=synth_gap_count,
        hedge_same_bar_tagged_structures=hedge_same_count,
        synthetic_same_bar_tagged_structures=synth_same_count,
        hedge_entry_fill_sides=hedge.row.entry_fill_sides,
        hedge_exit_fill_sides=hedge.row.exit_fill_sides,
        synthetic_entry_fill_sides=synthetic.row.entry_fill_sides,
        synthetic_exit_fill_sides=synthetic.row.exit_fill_sides,
    )


def _tagged_gross(run: _ModeRun, *, tag: Literal["gap", "same_bar"]) -> tuple[float, float, int]:
    results = {result.id: result for result in run.report.trade_pairs}
    selected: list[tuple[TradePairResult, Pair]] = []
    for pair in run.engine.pairs:
        is_gap = pair.entry_gap or pair.exit_gap
        matches = is_gap if tag == "gap" else pair.same_bar_resolved and not is_gap
        result = results.get(pair.id)
        if matches and result is not None:
            selected.append((result, pair))
    return (
        sum(_value(result.gross_pnl_pips) for result, _ in selected),
        sum(_pair_gross_r(result, pair, run.engine.params) for result, pair in selected),
        len(selected),
    )


def _pair_gross_r(
    result: TradePairResult, pair: Pair, params: EngineParams
) -> float:
    s_pips = pair.sl_dist / params.pip_size
    if s_pips <= 0:
        return 0.0
    legs = [result.primary, result.hedge, *result.unknown_legs]
    return sum(
        (leg.pnl_pips / s_pips) * (leg.qty / pair.qty)
        for leg in legs
        if leg is not None
    )


def _pair_cost_r(
    result: TradePairResult, pair: Pair, params: EngineParams
) -> float:
    s_pips = pair.sl_dist / params.pip_size
    if s_pips <= 0 or pair.qty <= 0:
        return 0.0
    legs = [result.primary, result.hedge, *result.unknown_legs]
    total = 0.0
    for leg in legs:
        if leg is None or leg.qty <= 0:
            continue
        weight = leg.qty / params.qty_ref
        total += (leg.cost_pips / weight / s_pips) * (leg.qty / pair.qty)
    return total


def _hold_hours(result: TradePairResult) -> float | None:
    legs = [result.primary, result.hedge, *result.unknown_legs]
    exits = [leg.exit_ts for leg in legs if leg is not None and leg.exit_ts is not None]
    if not exits:
        return None
    return (max(exits) - result.entry_ts).total_seconds() / 3600.0


def _candle_sha256(candles: list[Candle]) -> str:
    payload = json.dumps(
        [candle.model_dump(mode="json") for candle in candles],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses == 0:
        return None
    return gains / losses


def _win_rate_excl_be(values: list[float]) -> float | None:
    directional = [value for value in values if abs(value) > 1e-12]
    if not directional:
        return None
    return sum(value > 0 for value in directional) / len(directional)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _value(value: float | None) -> float:
    return 0.0 if value is None else value
