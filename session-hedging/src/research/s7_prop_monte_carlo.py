"""S7 PropGuard Monte Carlo using complete overlapping trade clusters."""

from __future__ import annotations

import math
import random
from statistics import mean, median
from typing import Any, Literal

from anchors import SessionAnchor
from cell_stats import candle_sha256, pair_cost_r, pair_gross_r
from engine import ClosedBarEngine
from models import Candle, EngineParams, EntryMode, Timeframe
from research import markdown
from research.scale import m1_coverage
from sessions import SessionWindow

S7_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
S7_SEED = 20260820
S7_SIMULATIONS = 2000
S7_HORIZON_DAYS = 100
S7_TARGET_PCT = 10.0
S7_DAILY_LIMITS = (3.0, 5.0)
S7_TOTAL_LIMITS = (6.0, 10.0)
S7_SPREAD_MEDIAN_PIPS = 2.0
S7_SPREAD_LOG_SIGMA = 0.35
S7_SLIPPAGE_MEAN_PIPS = 0.5
S7_GAP_FLOOR_PROBABILITY = 0.02
S7_GAP_MEAN_R = 0.25
S7_FALLBACK = "pessimistic_same_bar_no_subpath"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "p01": _percentile(values, 0.01),
        "p05": _percentile(values, 0.05),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
        "mean": mean(values),
    }


def _structure_rows(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    mode: EntryMode,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cell_params = EngineParams.model_validate(params.model_dump() | {"entry_mode": mode})
    engine = ClosedBarEngine(windows, cell_params, anchors, m1_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(update={"bar_count": len(candles)})
    pairs = {pair.id: pair for pair in engine.pairs}
    raw: list[dict[str, Any]] = []
    for result in report.trade_pairs:
        if result.status != "closed":
            continue
        pair = pairs[result.id]
        exits = [
            leg.exit_ts
            for leg in (result.primary, result.hedge, *result.unknown_legs)
            if leg is not None and leg.exit_ts is not None
        ]
        if not exits or pair.sl_dist <= 0:
            continue
        legs = [
            leg
            for leg in (result.primary, result.hedge, *result.unknown_legs)
            if leg is not None and leg.exit_ts is not None
        ]
        gross_r = pair_gross_r(result, pair, cell_params)
        raw.append(
            {
                "structure_id": result.id,
                "session": result.session,
                "entry_ts": result.entry_ts,
                "exit_ts": max(exits),
                "gross_pips": float(result.gross_pnl_pips or 0.0),
                "net_pips": float(result.net_pnl_pips or 0.0),
                "gross_r": gross_r,
                "net_r": gross_r - pair_cost_r(result, pair, cell_params),
                "stop_pips": pair.sl_dist / cell_params.pip_size,
                "transaction_sides": 2 * len(legs),
                "entry_gap": result.entry_gap,
                "exit_gap": result.exit_gap,
            }
        )
    stops = sorted(row["stop_pips"] for row in raw)
    low_edge = _percentile(stops, 1 / 3) if stops else 0.0
    high_edge = _percentile(stops, 2 / 3) if stops else 0.0
    for row in raw:
        if row["stop_pips"] <= low_edge:
            row["volatility_regime"] = "low"
        elif row["stop_pips"] <= high_edge:
            row["volatility_regime"] = "mid"
        else:
            row["volatility_regime"] = "high"
    identity = {
        "completed_structures": len(raw),
        "gross_pips": report.gross_equity_pips,
        "net_pips": report.net_equity_pips,
        "gross_r": report.gross_equity_r,
        "net_r": report.net_equity_r,
        "execution_cost_pips": report.execution_cost_pips,
        "financing_cost_pips": report.financing_cost_pips,
        "volatility_tercile_edges_stop_pips": [low_edge, high_edge],
    }
    return raw, identity


def build_trade_clusters(structures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build overlap components, then retain consecutive same-regime components together."""
    if not structures:
        return []
    ordered = sorted(structures, key=lambda row: (row["entry_ts"], row["structure_id"]))
    overlap_components: list[list[dict[str, Any]]] = []
    current = [ordered[0]]
    current_end = ordered[0]["exit_ts"]
    for row in ordered[1:]:
        if row["entry_ts"] <= current_end:
            current.append(row)
            current_end = max(current_end, row["exit_ts"])
        else:
            overlap_components.append(current)
            current = [row]
            current_end = row["exit_ts"]
    overlap_components.append(current)

    def regime(component: list[dict[str, Any]]) -> str:
        counts = {
            name: sum(row["volatility_regime"] == name for row in component)
            for name in ("low", "mid", "high")
        }
        return max(counts, key=lambda name: (counts[name], -("low", "mid", "high").index(name)))

    regime_blocks: list[list[dict[str, Any]]] = []
    block: list[dict[str, Any]] = []
    block_regime: str | None = None
    for component in overlap_components:
        component_regime = regime(component)
        if block and component_regime != block_regime:
            regime_blocks.append(block)
            block = []
        block.extend(component)
        block_regime = component_regime
    if block:
        regime_blocks.append(block)

    clusters: list[dict[str, Any]] = []
    for index, rows in enumerate(regime_blocks):
        start = min(row["entry_ts"] for row in rows)
        end = max(row["exit_ts"] for row in rows)
        duration_days = max(1, math.ceil((end - start).total_seconds() / 86400))
        timeline = []
        for row in rows:
            serialized = dict(row)
            serialized["entry_offset_hours"] = (row["entry_ts"] - start).total_seconds() / 3600
            serialized["exit_offset_hours"] = (row["exit_ts"] - start).total_seconds() / 3600
            serialized["entry_ts"] = row["entry_ts"].isoformat()
            serialized["exit_ts"] = row["exit_ts"].isoformat()
            timeline.append(serialized)
        max_concurrent = 0
        for row in rows:
            concurrent = sum(
                other["entry_ts"] <= row["entry_ts"] <= other["exit_ts"] for other in rows
            )
            max_concurrent = max(max_concurrent, concurrent)
        clusters.append(
            {
                "cluster_id": index,
                "volatility_regime": regime(rows),
                "start_ts": start.isoformat(),
                "end_ts": end.isoformat(),
                "duration_days": duration_days,
                "structure_count": len(rows),
                "sessions": sorted({row["session"] for row in rows}),
                "max_concurrent_structures": max_concurrent,
                "structure_ids": [row["structure_id"] for row in timeline],
                "structures": timeline,
            }
        )
    return clusters


def _sample_tail_cost(rng: random.Random) -> tuple[float, float]:
    spread = min(8.0, rng.lognormvariate(math.log(S7_SPREAD_MEDIAN_PIPS), S7_SPREAD_LOG_SIGMA))
    slippage = min(5.0, rng.expovariate(1 / S7_SLIPPAGE_MEAN_PIPS))
    return spread, slippage


def _simulate_path(
    clusters: list[dict[str, Any]],
    rng: random.Random,
    *,
    horizon_days: int,
    risk_pct_per_r: float,
    exposure_pct_per_structure: float,
    gap_probability: float,
) -> dict[str, Any]:
    events: dict[int, list[dict[str, Any]]] = {}
    exposure: dict[int, int] = {}
    cursor = 0
    sampled_clusters = 0
    while cursor < horizon_days:
        cluster = rng.choice(clusters)
        sampled_clusters += 1
        for structure in cluster["structures"]:
            entry_day = cursor + int(structure["entry_offset_hours"] // 24)
            exit_day = cursor + int(structure["exit_offset_hours"] // 24)
            for day in range(max(0, entry_day), min(horizon_days - 1, exit_day) + 1):
                exposure[day] = exposure.get(day, 0) + 1
            if 0 <= exit_day < horizon_days:
                events.setdefault(exit_day, []).append(structure)
        cursor += cluster["duration_days"]

    equity = 100.0
    min_free_margin = 100.0
    daily_breach_days = {limit: None for limit in S7_DAILY_LIMITS}
    total_breach_days = {limit: None for limit in S7_TOTAL_LIMITS}
    target_day = None
    gross_pips = net_pips = gross_r = net_r = 0.0
    spread_cost_pips = slippage_cost_pips = gap_cost_pips = 0.0
    gap_events = 0
    max_concurrent = 0
    for day in range(horizon_days):
        day_start_equity = equity
        active = exposure.get(day, 0)
        max_concurrent = max(max_concurrent, active)
        min_free_margin = min(min_free_margin, equity - active * exposure_pct_per_structure)
        for structure in events.get(day, []):
            spread, slippage = _sample_tail_cost(rng)
            sides = structure["transaction_sides"]
            spread_cost = spread * sides
            slippage_cost = slippage * sides
            gap_r = 0.0
            if structure["net_r"] < 0 and rng.random() < gap_probability:
                gap_r = min(2.0, rng.expovariate(1 / S7_GAP_MEAN_R))
                gap_events += 1
            gap_pips = gap_r * structure["stop_pips"]
            adjusted_net_pips = structure["net_pips"] - spread_cost - slippage_cost - gap_pips
            adjusted_net_r = (
                structure["net_r"]
                - (spread_cost + slippage_cost) / structure["stop_pips"]
                - gap_r
            )
            gross_pips += structure["gross_pips"]
            net_pips += adjusted_net_pips
            gross_r += structure["gross_r"]
            net_r += adjusted_net_r
            spread_cost_pips += spread_cost
            slippage_cost_pips += slippage_cost
            gap_cost_pips += gap_pips
            equity += adjusted_net_r * risk_pct_per_r
            min_free_margin = min(min_free_margin, equity - active * exposure_pct_per_structure)
            for limit in S7_DAILY_LIMITS:
                if daily_breach_days[limit] is None and equity <= day_start_equity - limit:
                    daily_breach_days[limit] = day + 1
            for limit in S7_TOTAL_LIMITS:
                if total_breach_days[limit] is None and equity <= 100.0 - limit:
                    total_breach_days[limit] = day + 1
            if target_day is None and equity >= 100.0 + S7_TARGET_PCT:
                target_day = day + 1
    return {
        "sampled_clusters": sampled_clusters,
        "gross_pips": gross_pips,
        "net_pips": net_pips,
        "gross_r": gross_r,
        "net_r": net_r,
        "spread_cost_pips": spread_cost_pips,
        "slippage_cost_pips": slippage_cost_pips,
        "gap_cost_pips": gap_cost_pips,
        "gap_events": gap_events,
        "max_concurrent_structures": max_concurrent,
        "minimum_free_margin_pct": min_free_margin,
        "daily_breach_days": {str(int(key)): value for key, value in daily_breach_days.items()},
        "total_breach_days": {str(int(key)): value for key, value in total_breach_days.items()},
        "target_day": target_day,
    }


def _limit_summary(
    paths: list[dict[str, Any]], key: str, limits: tuple[float, ...]
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for limit in limits:
        days = [path[key][str(int(limit))] for path in paths]
        observed = [day for day in days if day is not None]
        summary[str(int(limit))] = {
            "limit_pct": limit,
            "breach_count": len(observed),
            "breach_probability": len(observed) / len(paths),
            "expected_days_to_breach_conditional": mean(observed) if observed else None,
            "median_days_to_breach_conditional": median(observed) if observed else None,
        }
    return summary


def _run_mode_simulation(
    clusters: list[dict[str, Any]],
    *,
    seed: int,
    simulations: int,
    horizon_days: int,
    risk_pct_per_r: float,
    exposure_pct_per_structure: float,
    gap_probability: float,
) -> dict[str, Any]:
    rng = random.Random(seed)
    paths = [
        _simulate_path(
            clusters,
            rng,
            horizon_days=horizon_days,
            risk_pct_per_r=risk_pct_per_r,
            exposure_pct_per_structure=exposure_pct_per_structure,
            gap_probability=gap_probability,
        )
        for _ in range(simulations)
    ]
    target_days = [path["target_day"] for path in paths if path["target_day"] is not None]
    return {
        "seed": seed,
        "simulation_count": simulations,
        "horizon_days": horizon_days,
        "daily_limit_breaches": _limit_summary(paths, "daily_breach_days", S7_DAILY_LIMITS),
        "total_limit_breaches": _limit_summary(paths, "total_breach_days", S7_TOTAL_LIMITS),
        "target_pct": S7_TARGET_PCT,
        "target_count": len(target_days),
        "target_probability": len(target_days) / simulations,
        "expected_time_to_target_days_conditional": mean(target_days) if target_days else None,
        "median_time_to_target_days_conditional": median(target_days) if target_days else None,
        "minimum_free_margin_pct_distribution": _distribution(
            [path["minimum_free_margin_pct"] for path in paths]
        ),
        "max_concurrent_structures_distribution": _distribution(
            [float(path["max_concurrent_structures"]) for path in paths]
        ),
        "path_gross_pips_distribution": _distribution([path["gross_pips"] for path in paths]),
        "path_net_pips_distribution": _distribution([path["net_pips"] for path in paths]),
        "path_gross_r_distribution": _distribution([path["gross_r"] for path in paths]),
        "path_net_r_distribution": _distribution([path["net_r"] for path in paths]),
        "spread_cost_pips_distribution": _distribution(
            [path["spread_cost_pips"] for path in paths]
        ),
        "slippage_cost_pips_distribution": _distribution(
            [path["slippage_cost_pips"] for path in paths]
        ),
        "gap_cost_pips_distribution": _distribution([path["gap_cost_pips"] for path in paths]),
        "gap_event_count_distribution": _distribution(
            [float(path["gap_events"]) for path in paths]
        ),
    }


def run_s7_prop_monte_carlo(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
    seed: int = S7_SEED,
    simulations: int = S7_SIMULATIONS,
    horizon_days: int = S7_HORIZON_DAYS,
) -> dict[str, Any]:
    """Bootstrap complete overlap/regime clusters and stress every incumbent mode."""
    if not candles:
        raise ValueError("S7 requires candles")
    if simulations <= 0 or horizon_days <= 0:
        raise ValueError("S7 simulations and horizon must be positive")
    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = (m1_bars or []) if coverage.status == "complete" else []
    modes: list[dict[str, Any]] = []
    for index, mode in enumerate(S7_MODES):
        structures, baseline = _structure_rows(
            candles,
            windows,
            params,
            anchors,
            mode=mode,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            m1_bars=subpath_bars,
        )
        clusters = build_trade_clusters(structures)
        if not clusters:
            raise ValueError(f"S7 {mode.value} produced no complete trade clusters")
        empirical_gap_probability = (
            sum(row["entry_gap"] or row["exit_gap"] for row in structures) / len(structures)
        )
        gap_probability = max(S7_GAP_FLOOR_PROBABILITY, empirical_gap_probability)
        mode_seed = seed + index * 1_000_003
        simulation = _run_mode_simulation(
            clusters,
            seed=mode_seed,
            simulations=simulations,
            horizon_days=horizon_days,
            risk_pct_per_r=params.risk_pct_per_r,
            exposure_pct_per_structure=params.max_pair_risk_pct,
            gap_probability=gap_probability,
        )
        modes.append(
            {
                "entry_mode": mode.value,
                "baseline": baseline,
                "complete_structure_count": len(structures),
                "cluster_count": len(clusters),
                "multi_structure_cluster_count": sum(
                    cluster["structure_count"] > 1 for cluster in clusters
                ),
                "cluster_structure_count_distribution": _distribution(
                    [float(cluster["structure_count"]) for cluster in clusters]
                ),
                "cluster_regime_counts": {
                    name: sum(cluster["volatility_regime"] == name for cluster in clusters)
                    for name in ("low", "mid", "high")
                },
                "empirical_gap_probability": empirical_gap_probability,
                "simulated_gap_probability": gap_probability,
                "clusters": clusters,
                "simulation": simulation,
            }
        )
    return {
        "study": "s7_propguard_monte_carlo",
        "symbol": symbol,
        "timeframe": timeframe.value,
        "source": source,
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat(),
        "last_bar_ts": candles[-1].ts.isoformat(),
        "candle_set_sha256": candle_sha256(candles),
        "m1_coverage": coverage.model_dump(mode="json"),
        "seed": seed,
        "simulation_count_per_mode": simulations,
        "horizon_days": horizon_days,
        "resampling": {
            "unit": "complete_trade_cluster",
            "individual_legs_resampled": False,
            "overlap_rule": "connected components of overlapping structure holding intervals",
            "regime_rule": "consecutive overlap components with the same stop-distance "
            "volatility tercile remain one bootstrap block",
            "preserves_london_new_york_overlap": True,
            "preserves_volatility_regime_clustering": True,
        },
        "tail_model": {
            "spread_pips_per_side": {
                "distribution": "lognormal_capped",
                "median": S7_SPREAD_MEDIAN_PIPS,
                "log_sigma": S7_SPREAD_LOG_SIGMA,
                "cap": 8.0,
            },
            "slippage_pips_per_side": {
                "distribution": "exponential_capped",
                "mean": S7_SLIPPAGE_MEAN_PIPS,
                "cap": 5.0,
            },
            "gap_stop_extra_r": {
                "distribution": "exponential_capped",
                "mean": S7_GAP_MEAN_R,
                "cap": 2.0,
                "probability": "max(empirical_gap_rate, 2%) on losing structures",
            },
        },
        "firm_model": {
            "initial_equity_pct": 100.0,
            "risk_pct_per_r": params.risk_pct_per_r,
            "concurrent_exposure_pct_per_structure": params.max_pair_risk_pct,
            "daily_loss_limits_pct": list(S7_DAILY_LIMITS),
            "total_loss_limits_pct": list(S7_TOTAL_LIMITS),
            "target_pct": S7_TARGET_PCT,
            "minimum_free_margin_definition": (
                "normalized equity percent minus concurrent structures times configured "
                "MAX_PAIR_RISK_PCT; a risk-budget proxy, not broker margin"
            ),
        },
        "modes": modes,
        "data_sufficiency": {
            "harness_verified": True,
            "prop_survivability_claim_supported": False,
            "reason": "2,000 M15 bars cover roughly 30 days of one symbol.",
            "needed": "Multiple years of complete trade clusters plus covering M1 and broker "
            "bid/ask, slippage, gap, swap, contract and margin observations across regimes.",
        },
    }


def render_s7_markdown(report: dict[str, Any]) -> str:
    coverage = report["m1_coverage"]
    lines = [
        "# S7 PropGuard cluster Monte Carlo",
        "",
        "Complete structures are grouped into overlapping trade clusters, and consecutive "
        "same-volatility-regime components remain one bootstrap block. Individual legs are never "
        "resampled. This preserves London/New York overlap, concurrent exposure and local regime "
        "clustering inside every sampled block.",
        "",
        "## Identity, seed and limitations",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol / timeframe | {report['symbol']} / {report['timeframe']} |",
        f"| Bars | {report['bar_count']} |",
        f"| Bounds | {report['first_bar_ts']} to {report['last_bar_ts']} |",
        f"| Fingerprint | `{report['candle_set_sha256']}` |",
        f"| Seed | **{report['seed']}** |",
        f"| Simulations per mode / horizon | {report['simulation_count_per_mode']} / "
        f"{report['horizon_days']} days |",
        f"| M1 coverage | {coverage['status']}: {coverage['covered_parent_bars']} / "
        f"{coverage['total_parent_bars']} ({coverage['covered_parent_fraction']:.2%}) |",
        f"| Uniform fallback | `{coverage['subpath_fallback'] or 'none'}` |",
        "",
        "The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies deterministic "
        "cluster resampling and tail simulation; it cannot support a prop-survivability claim. "
        "That requires multiple years of clusters plus covering M1 and broker bid/ask, slippage, "
        "gap, swap, contract and margin observations across regimes. Partial M1 chronology is not "
        f"mixed; the full run uses `{coverage['subpath_fallback'] or S7_FALLBACK}`.",
        "",
        "## Breach and target results",
        "",
    ]
    rows: list[list[str]] = []
    for mode in report["modes"]:
        simulation = mode["simulation"]
        daily = simulation["daily_limit_breaches"]
        total = simulation["total_limit_breaches"]
        free = simulation["minimum_free_margin_pct_distribution"]
        rows.append(
            [
                mode["entry_mode"],
                str(mode["complete_structure_count"]),
                str(mode["cluster_count"]),
                markdown.pct(daily["3"]["breach_probability"], 2),
                markdown.num(daily["3"]["expected_days_to_breach_conditional"], 2),
                markdown.pct(daily["5"]["breach_probability"], 2),
                markdown.num(daily["5"]["expected_days_to_breach_conditional"], 2),
                markdown.pct(total["6"]["breach_probability"], 2),
                markdown.num(total["6"]["expected_days_to_breach_conditional"], 2),
                markdown.pct(total["10"]["breach_probability"], 2),
                markdown.num(total["10"]["expected_days_to_breach_conditional"], 2),
                markdown.pct(simulation["target_probability"], 2),
                markdown.num(simulation["expected_time_to_target_days_conditional"], 2),
                markdown.num(free["p01"], 2),
                markdown.num(free["p50"], 2),
            ]
        )
    lines += markdown.table(
        [
            "Mode",
            "Structures",
            "Clusters",
            "P daily 3%",
            "Days to 3%",
            "P daily 5%",
            "Days to 5%",
            "P total 6%",
            "Days to 6%",
            "P total 10%",
            "Days to 10%",
            "P target",
            "Days to target",
            "Min free margin p01",
            "Min free margin p50",
        ],
        rows,
        align_right_from=1,
    )
    lines += [
        "Expected days are conditional on the breach or target occurring inside the 100-day "
        "horizon. Minimum free margin is normalized equity percent minus concurrent structures "
        "times `MAX_PAIR_RISK_PCT`; it is explicitly a risk-budget proxy, not broker margin.",
        "",
        "## Gross/net path distributions and tail costs",
        "",
    ]
    path_rows = []
    for mode in report["modes"]:
        simulation = mode["simulation"]
        gross_pips = simulation["path_gross_pips_distribution"]
        net_pips = simulation["path_net_pips_distribution"]
        gross_r = simulation["path_gross_r_distribution"]
        net_r = simulation["path_net_r_distribution"]
        path_rows.append(
            [
                mode["entry_mode"],
                f"{gross_pips['p05']:.2f} / {gross_pips['p50']:.2f} / {gross_pips['p95']:.2f}",
                f"{net_pips['p05']:.2f} / {net_pips['p50']:.2f} / {net_pips['p95']:.2f}",
                f"{gross_r['p05']:.4f} / {gross_r['p50']:.4f} / {gross_r['p95']:.4f}",
                f"{net_r['p05']:.4f} / {net_r['p50']:.4f} / {net_r['p95']:.4f}",
                markdown.num(simulation["spread_cost_pips_distribution"]["mean"]),
                markdown.num(simulation["slippage_cost_pips_distribution"]["mean"]),
                markdown.num(simulation["gap_cost_pips_distribution"]["mean"]),
            ]
        )
    lines += markdown.table(
        [
            "Mode",
            "Gross pips p05/p50/p95",
            "Net pips p05/p50/p95",
            "Gross R p05/p50/p95",
            "Net R p05/p50/p95",
            "Mean spread cost",
            "Mean slippage cost",
            "Mean gap cost",
        ],
        path_rows,
        align_right_from=5,
    )
    lines += [
        "## Every empirical trade cluster",
        "",
        "Every cluster and every complete structure ID is printed below; no leg-level sampling or "
        "winning-cluster filtering occurs.",
        "",
    ]
    cluster_rows = []
    for mode in report["modes"]:
        for cluster in mode["clusters"]:
            cluster_rows.append(
                [
                    mode["entry_mode"],
                    str(cluster["cluster_id"]),
                    cluster["volatility_regime"],
                    str(cluster["duration_days"]),
                    str(cluster["structure_count"]),
                    ", ".join(cluster["sessions"]),
                    str(cluster["max_concurrent_structures"]),
                    ", ".join(cluster["structure_ids"]),
                ]
            )
    lines += markdown.table(
        [
            "Mode",
            "Cluster",
            "Regime",
            "Days",
            "Structures",
            "Sessions",
            "Max concurrent",
            "Structure IDs",
        ],
        cluster_rows,
        align_right_from=3,
    )
    return "\n".join(lines).rstrip() + "\n"
