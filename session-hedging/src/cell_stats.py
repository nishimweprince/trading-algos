"""Per-cell structure statistics shared by the Phase 2 comparison and the S8 sweep.

One completed structure is one closed pair. Gross and net are always carried
together: net subtracts the modelled execution and financing cost of every leg.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from statistics import median

from engine import ClosedBarEngine, Pair
from metrics import OutcomeKind, classify_pair
from models import BacktestReport, Candle, EngineParams, TradePairResult


@dataclass(frozen=True, slots=True)
class CompletedStructure:
    """One closed structure with its paired gross/net pips, R, and hold time."""

    id: str
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    hold_hours: float | None


def completed_structures(
    engine: ClosedBarEngine, report: BacktestReport
) -> list[CompletedStructure]:
    pairs = {pair.id: pair for pair in engine.pairs}
    completed: list[CompletedStructure] = []
    for result in report.trade_pairs:
        if result.status != "closed":
            continue
        pair = pairs[result.id]
        gross_r = pair_gross_r(result, pair, engine.params)
        completed.append(
            CompletedStructure(
                id=result.id,
                gross_pips=value(result.gross_pnl_pips),
                net_pips=value(result.net_pnl_pips),
                gross_r=gross_r,
                net_r=gross_r - pair_cost_r(result, pair, engine.params),
                hold_hours=hold_hours(result),
            )
        )
    return completed


def shared_cell_metrics(
    engine: ClosedBarEngine,
    report: BacktestReport,
    completed: list[CompletedStructure],
) -> dict[str, object]:
    """Build the metric fields common to a comparison row and an S8 sweep cell."""
    gross_pips = [structure.gross_pips for structure in completed]
    net_pips = [structure.net_pips for structure in completed]
    gross_rs = [structure.gross_r for structure in completed]
    net_rs = [structure.net_r for structure in completed]
    hold_values = [
        structure.hold_hours for structure in completed if structure.hold_hours is not None
    ]
    exit_fill_sides = len(engine.trades)
    cancelled = [event for event in report.events if event.kind == "entry_order_cancelled"]

    return {
        "completed_structures": len(completed),
        "gross_pips": report.gross_equity_pips,
        "net_pips": report.net_equity_pips,
        "gross_r": report.gross_equity_r,
        "net_r": report.net_equity_r,
        "execution_cost_pips": report.execution_cost_pips,
        "financing_cost_pips": report.financing_cost_pips,
        "total_cost_pips": report.equity_cost_pips,
        "gross_expectancy_pips": mean(gross_pips),
        "net_expectancy_pips": mean(net_pips),
        "gross_expectancy_r": mean(gross_rs),
        "net_expectancy_r": mean(net_rs),
        "gross_profit_factor": profit_factor(gross_pips),
        "net_profit_factor": profit_factor(net_pips),
        "gross_win_rate_excl_be": win_rate_excl_be(gross_pips),
        "net_win_rate_excl_be": win_rate_excl_be(net_pips),
        "survivor_tp_rate": report.survivor_tp_rate,
        "breakeven_tp_rate_required": report.breakeven_tp_rate_required,
        "gross_max_drawdown_pips": report.gross_max_drawdown_pips,
        "net_max_drawdown_pips": report.net_max_drawdown_pips,
        "gross_max_drawdown_r": report.gross_max_drawdown_r,
        "net_max_drawdown_r": report.net_max_drawdown_r,
        "breakeven_pips_per_completed_side": report.breakeven_pips_per_side,
        "transaction_sides": report.transaction_sides,
        "cost_side_equivalents": report.cost_side_equivalents,
        "entry_fill_sides": report.transaction_sides - exit_fill_sides,
        "exit_fill_sides": exit_fill_sides,
        "cancelled_entry_orders": len(cancelled),
        "expired_entry_orders": sum(
            event.detail.get("reason") == "expired" for event in cancelled
        ),
        "median_hold_hours": float(median(hold_values)) if hold_values else None,
        "p95_hold_hours": percentile(hold_values, 0.95),
        "max_concurrent_structures": report.max_concurrent_structures,
        "suppressed_signals": report.suppressed_signal_count,
        "unresolved_structures": report.unresolved_structures,
        "prop_guard_breached": report.prop_guard_breached,
        "prop_guard_breach_reason": report.prop_guard_breach_reason,
        "prop_guard_breached_at": report.prop_guard_breached_at,
        "prop_guard_breach_events": sum(
            event.kind == "prop_guard_breached" for event in report.events
        ),
    }


def pair_gross_r(result: TradePairResult, pair: Pair, params: EngineParams) -> float:
    s_pips = pair.sl_dist / params.pip_size
    if s_pips <= 0:
        return 0.0
    legs = [result.primary, result.hedge, *result.unknown_legs]
    return sum(
        (leg.pnl_pips / s_pips) * (leg.qty / pair.qty) for leg in legs if leg is not None
    )


def pair_cost_r(result: TradePairResult, pair: Pair, params: EngineParams) -> float:
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


def hold_hours(result: TradePairResult) -> float | None:
    legs = [result.primary, result.hedge, *result.unknown_legs]
    exits = [leg.exit_ts for leg in legs if leg is not None and leg.exit_ts is not None]
    if not exits:
        return None
    return (max(exits) - result.entry_ts).total_seconds() / 3600.0


def candle_sha256(candles: list[Candle]) -> str:
    payload = json.dumps(
        [candle.model_dump(mode="json") for candle in candles],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses == 0:
        return None
    return gains / losses


def win_rate_excl_be(values: list[float]) -> float | None:
    directional = [value for value in values if abs(value) > 1e-12]
    if not directional:
        return None
    return sum(value > 0 for value in directional) / len(directional)


def percentile(values: list[float], fraction: float) -> float | None:
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


def value(raw: float | None) -> float:
    return 0.0 if raw is None else raw


def pair_outcome(result: TradePairResult, pair: Pair, params: EngineParams) -> OutcomeKind:
    """Classify one structure exactly as the headline metrics do."""
    legs = {
        leg.side: leg
        for leg in (result.primary, result.hedge, *result.unknown_legs)
        if leg is not None
    }
    s_pips = pair.sl_dist / params.pip_size if pair.sl_dist else 0.0
    pair_r = sum(leg.pnl_pips for leg in legs.values()) / s_pips if s_pips else 0.0
    reasons = {leg.reason for leg in legs.values() if leg.reason is not None}
    return classify_pair(
        locked=pair.locked,
        same_bar=pair.same_bar_resolved,
        long_bucket=legs["long"].bucket if "long" in legs else None,
        short_bucket=legs["short"].bucket if "short" in legs else None,
        pair_r=pair_r,
        time_exit="time_exit" in reasons,
    )
