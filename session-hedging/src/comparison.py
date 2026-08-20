"""Four-mode comparison over one immutable candle set and shared configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from anchors import SessionAnchor
from cell_stats import (
    candle_sha256,
    completed_structures,
    pair_gross_r,
    shared_cell_metrics,
    value,
)
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
        candle_set_sha256=candle_sha256(candles),
        shared_params=shared_params,
        rows=[runs[mode].row for mode in COMPARISON_MODES],
        hedge_vs_synthetic=_attribution(
            runs[EntryMode.HEDGE_PAIR], runs[EntryMode.SYNTHETIC_BREAKOUT]
        ),
    )


def _comparison_row(
    engine: ClosedBarEngine, report: BacktestReport
) -> EntryModeComparisonRow:
    completed = completed_structures(engine, report)
    return EntryModeComparisonRow(
        entry_mode=report.entry_mode,
        **shared_cell_metrics(engine, report, completed),
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
        sum(value(result.gross_pnl_pips) for result, _ in selected),
        sum(pair_gross_r(result, pair, run.engine.params) for result, pair in selected),
        len(selected),
    )
