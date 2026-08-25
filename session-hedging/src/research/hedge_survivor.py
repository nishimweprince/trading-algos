"""Frozen hedge-pair survivor-policy development programme.

The known 2024-12-10..2026-08-20 H1 set is development-only. This module freezes
the candidate family before any external holdout is opened and evaluates every
candidate both with ordinary portfolio suppression and with matched opportunities.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from anchors import SessionAnchor
from cell_stats import candle_sha256, completed_structures, shared_cell_metrics
from engine import ClosedBarEngine
from models import Candle, EngineParams, Timeframe
from research.phase3_exploratory import bootstrap_lower_bound
from sessions import SessionWindow

PROGRAMME_VERSION = 1
DEVELOPMENT_FIRST_TS = "2024-12-10T02:00:00+00:00"
DEVELOPMENT_LAST_TS = "2026-08-20T10:00:00+00:00"
MIN_EXTERNAL_HOLDOUT_YEARS = 3
BOOTSTRAP_RESAMPLES = 2_000
BOOTSTRAP_SEED = 20260825


def _candidate(
    candidate_id: str,
    *,
    survivor_exit_mode: str,
    lock_mode: str = "absolute",
    tp_mode: str = "fixed_r",
    activation_r: float = 1.5,
    gap_r: float = 1.0,
    complexity_rank: int = 0,
) -> dict[str, object]:
    return {
        "id": candidate_id,
        "entry_mode": "hedge_pair",
        "hedge_path_mode": "chronological_v2",
        "survivor_exit_mode": survivor_exit_mode,
        "survivor_trail_activation_r": activation_r,
        "survivor_trail_gap_r": gap_r,
        "lock_mode": lock_mode,
        "lock_pips": 20.0,
        "tp_mode": tp_mode,
        "partial_tp_r": 1.0,
        "partial_fraction": 0.5,
        "complexity_rank": complexity_rank,
    }


SURVIVOR_CANDIDATES: tuple[dict[str, object], ...] = (
    _candidate("incumbent:chronological:lock20", survivor_exit_mode="legacy_lock"),
    _candidate("survivor:unlocked", survivor_exit_mode="unlocked"),
    _candidate(
        "survivor:breakeven",
        survivor_exit_mode="legacy_lock",
        lock_mode="breakeven",
        complexity_rank=1,
    ),
    _candidate(
        "survivor:partial50-at-1r",
        survivor_exit_mode="legacy_lock",
        tp_mode="partial_trail",
        complexity_rank=2,
    ),
    *tuple(
        _candidate(
            f"survivor:mfe-trail:a{activation:g}:g{gap:g}",
            survivor_exit_mode="mfe_trail",
            activation_r=activation,
            gap_r=gap,
            complexity_rank=3,
        )
        for activation in (1.25, 1.5, 2.0)
        for gap in (1.0, 1.5)
    ),
)


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def candidate_sha256() -> str:
    return hashlib.sha256(canonical_json(SURVIVOR_CANDIDATES).encode()).hexdigest()


def survivor_candidate_manifest() -> dict[str, object]:
    return {
        "programme": "hedge_pair_survivor_management",
        "programme_version": PROGRAMME_VERSION,
        "candidate_sha256": candidate_sha256(),
        "candidate_count": len(SURVIVOR_CANDIDATES),
        "candidates": list(SURVIVOR_CANDIDATES),
        "known_development_period": {
            "first_bar_ts": DEVELOPMENT_FIRST_TS,
            "last_bar_ts": DEVELOPMENT_LAST_TS,
            "data_role": "development_only_already_inspected",
        },
        "external_holdout": {
            "status": "locked",
            "minimum_years": MIN_EXTERNAL_HOLDOUT_YEARS,
            "requires_complete_m1": True,
            "opened": False,
        },
        "fixed_entry_rules": {
            "timeframe": "H1",
            "sessions": ["tokyo", "london", "new_york"],
            "orb_minutes": 60,
            "entry_delay_minutes": 15,
            "stop_mode": "bar_range",
            "sl_mult": 2.0,
            "rr": 3.0,
            "time_exit_mode": "max_age",
            "max_age_hours": 24.0,
        },
        "selection_rule": (
            "highest one-sided 90% block-bootstrap lower net expectancy R; "
            "then lower net max drawdown R; then simpler policy; then candidate id"
        ),
        "promotion_analysis": {
            "walk_forward": "research.s6_walk_forward",
            "block_bootstrap": "research.phase3_exploratory.bootstrap_lower_bound",
            "dsr_and_pbo": "research.s6_walk_forward",
            "monte_carlo": "research.s7_prop_monte_carlo",
            "deterministic_artifacts": True,
        },
    }


def _params_for(
    base: EngineParams,
    candidate: dict[str, object],
    *,
    replay: Literal["portfolio", "matched_opportunity"],
    stress_costs: bool,
) -> EngineParams:
    updates: dict[str, object] = {
        "entry_mode": "hedge_pair",
        "risk_mode": "fixed_fractional",
        "risk_pct_per_r": 0.10,
        "max_pair_risk_pct": 0.20,
        "dollars_per_pip_per_qty": base.dollars_per_pip_per_qty or 10.0,
        "timeframe_minutes": 60,
        "orb_minutes": 60,
        "entry_delay_minutes": 15,
        "stop_mode": "bar_range",
        "sl_mult": 2.0,
        "rr": 3.0,
        "time_exit_mode": "max_age",
        "max_age_hours": 24.0,
        "filter_d1_ema50": False,
        "filter_nr7": False,
        "filter_orb_atr_min": 0.0,
        "filter_orb_atr_max": 0.0,
        "entry_hours_utc_exclude": [],
    } | {key: value for key, value in candidate.items() if key not in {"id", "complexity_rank"}}
    if replay == "matched_opportunity":
        updates |= {
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0.0,
        }
    if stress_costs:
        for field in ("spread_pips_per_side", "slippage_pips_per_side"):
            updates[field] = 2.0 * float(getattr(base, field))
    return EngineParams.model_validate(base.model_dump() | updates)


def _evaluate(
    candles: list[Candle],
    windows: list[SessionWindow],
    base: EngineParams,
    anchors: list[SessionAnchor],
    m1_bars: list[Candle],
    candidate: dict[str, object],
    *,
    replay: Literal["portfolio", "matched_opportunity"],
    stress_costs: bool,
    symbol: str,
    source: Literal["local", "ctrader"],
) -> dict[str, object]:
    params = _params_for(base, candidate, replay=replay, stress_costs=stress_costs)
    engine = ClosedBarEngine(windows, params, anchors, m1_bars)
    engine.run(candles)
    report = engine.report(symbol, Timeframe.H1, source).model_copy(
        update={"bar_count": len(candles), "candle_set_sha256": candle_sha256(candles)}
    )
    completed = completed_structures(engine, report)
    metrics = shared_cell_metrics(engine, report, completed)
    results = {result.id: result for result in report.trade_pairs}
    pairs = {pair.id: pair for pair in engine.pairs}
    returns: list[dict[str, object]] = []
    for structure in completed:
        result = results[structure.id]
        pair = pairs[structure.id]
        returns.append(
            {
                "structure_id": structure.id,
                "session": result.session,
                "weekday": result.weekday,
                "quarter": f"{result.entry_ts.year}-Q{(result.entry_ts.month - 1) // 3 + 1}",
                "survivor_side": pair.survivor_side,
                "stop_pips": result.stop_pips,
                "net_pips": structure.net_pips,
                "net_r": structure.net_r,
            }
        )
    net_rs = [float(item["net_r"]) for item in returns]
    entry_signal_ids = sorted(pair.id for pair in engine.pairs)
    return {
        "candidate_id": candidate["id"],
        "replay": replay,
        "cost_surface": "stress_2x" if stress_costs else "base",
        "costs_are_zero": report.report_header.costs_are_zero,
        "m1_fallback_count": report.report_header.m1_fallback_count,
        "gap_fill_structures": sum(
            result.entry_gap or result.exit_gap for result in report.trade_pairs
        ),
        **metrics,
        "bootstrap_lower_net_expectancy_r": bootstrap_lower_bound(
            net_rs, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED
        ),
        "entry_signal_count": len(entry_signal_ids),
        "entry_signal_sha256": hashlib.sha256(
            canonical_json(entry_signal_ids).encode()
        ).hexdigest(),
        "entry_signal_ids": entry_signal_ids,
        "structure_returns": returns,
    }


def run_survivor_development(
    candles: list[Candle],
    windows: list[SessionWindow],
    base: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str = "XAUUSD",
    source: Literal["local", "ctrader"] = "local",
    m1_bars: list[Candle] | None = None,
) -> dict[str, object]:
    if not candles:
        raise ValueError("survivor development requires candles")
    evaluations = [
        _evaluate(
            candles,
            windows,
            base,
            anchors,
            m1_bars or [],
            candidate,
            replay=replay,
            stress_costs=stress,
            symbol=symbol,
            source=source,
        )
        for candidate in SURVIVOR_CANDIDATES
        for replay in ("portfolio", "matched_opportunity")
        for stress in (False, True)
    ]
    selection_pool = [
        item
        for item in evaluations
        if item["replay"] == "portfolio" and item["cost_surface"] == "base"
    ]
    candidate_by_id = {str(item["id"]): item for item in SURVIVOR_CANDIDATES}

    def selection_key(item: dict[str, object]) -> tuple[float, float, int, str]:
        raw_bound = item["bootstrap_lower_net_expectancy_r"]
        bound = float("-inf") if raw_bound is None else float(raw_bound)
        candidate = candidate_by_id[str(item["candidate_id"])]
        return (
            -bound,
            float(item["net_max_drawdown_r"]),
            int(candidate["complexity_rank"]),
            str(item["candidate_id"]),
        )

    selected = min(
        selection_pool,
        key=selection_key,
    )
    matched_hashes = {
        str(item["entry_signal_sha256"])
        for item in evaluations
        if item["replay"] == "matched_opportunity"
    }
    common_signal_replay_passed = len(matched_hashes) == 1
    if not common_signal_replay_passed:
        raise ValueError("matched-opportunity candidates did not receive identical entry signals")

    incumbent_id = "incumbent:chronological:lock20"
    portfolio_base = {
        str(item["candidate_id"]): set(item["entry_signal_ids"])
        for item in evaluations
        if item["replay"] == "portfolio" and item["cost_surface"] == "base"
    }
    incumbent_signals = portfolio_base[incumbent_id]
    overlap_attribution = []
    for candidate_id, candidate_signals in sorted(portfolio_base.items()):
        union = incumbent_signals | candidate_signals
        overlap_attribution.append(
            {
                "candidate_id": candidate_id,
                "incumbent_id": incumbent_id,
                "shared_signals": len(incumbent_signals & candidate_signals),
                "candidate_only_signals": len(candidate_signals - incumbent_signals),
                "incumbent_only_signals": len(incumbent_signals - candidate_signals),
                "jaccard": (
                    len(incumbent_signals & candidate_signals) / len(union) if union else 1.0
                ),
            }
        )
    return {
        "study": "hedge_pair_survivor_development",
        "manifest": survivor_candidate_manifest(),
        "candle_set_sha256": candle_sha256(candles),
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat(),
        "last_bar_ts": candles[-1].ts.isoformat(),
        "selected_development_candidate": selected["candidate_id"],
        "common_signal_replay_passed": common_signal_replay_passed,
        "matched_entry_signal_sha256": next(iter(matched_hashes)),
        "overlap_attribution": overlap_attribution,
        "promotion_eligible": False,
        "promotion_blocker": (
            "External three-year holdout with complete covering M1 has not been opened."
        ),
        "evaluations": evaluations,
    }


def write_survivor_development(report: dict[str, object], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "hedge-pair-survivor-development.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def promotion_gate_results(
    *,
    holdout_net_r: float,
    holdout_profit_factor: float | None,
    stress_net_r: float,
    stress_profit_factor: float | None,
    positive_fold_fraction: float,
    bootstrap_lower_r: float | None,
    candidate_drawdown_r: float,
    incumbent_drawdown_r: float,
    dsr_probability: float,
    pbo: float,
    matched_net_r: float,
    max_concentration: float,
    m1_complete: bool,
    resolver_fallbacks: int,
    configured_costs_nonzero: bool,
) -> dict[str, object]:
    gates = {
        "base_holdout": holdout_net_r > 0 and (holdout_profit_factor or 0) > 1,
        "stress_cost": stress_net_r > 0 and (stress_profit_factor or 0) > 1,
        "fold_consistency": positive_fold_fraction >= 2 / 3,
        "bootstrap": bootstrap_lower_r is not None and bootstrap_lower_r > 0,
        "drawdown": candidate_drawdown_r <= 0.8 * incumbent_drawdown_r,
        "dsr": dsr_probability >= 0.95,
        "pbo": pbo <= 0.20,
        "matched_opportunity": matched_net_r > 0,
        "concentration": max_concentration < 0.70,
        "m1_coverage": m1_complete and resolver_fallbacks == 0,
        "configured_costs": configured_costs_nonzero,
    }
    return {"passed": all(gates.values()), "gates": gates}
