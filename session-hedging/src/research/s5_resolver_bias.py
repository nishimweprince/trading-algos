"""S5: quantify outcome sensitivity across the executable resolver ladder."""

from __future__ import annotations

from typing import Any, Literal

from anchors import SessionAnchor
from cell_stats import (
    candle_sha256,
    completed_structures,
    pair_cost_r,
    pair_gross_r,
    pair_outcome,
    shared_cell_metrics,
)
from engine import ClosedBarEngine
from models import Candle, EngineParams, IntrabarMode, Timeframe
from research import markdown
from research.scale import m1_coverage
from sessions import SessionWindow

S5_EXECUTABLE_TIERS: tuple[tuple[int, IntrabarMode], ...] = (
    (0, IntrabarMode.OPTIMISTIC),
    (1, IntrabarMode.PESSIMISTIC),
    (2, IntrabarMode.M1),
    (3, IntrabarMode.M1_CONSERVATIVE),
)
S5_TICK_TIER = 4
S5_FALLBACK = "pessimistic_same_bar_no_subpath"


def _value(raw: float | None) -> float:
    return 0.0 if raw is None else float(raw)


def _structure_snapshots(
    engine: ClosedBarEngine, report: Any
) -> dict[str, dict[str, Any]]:
    pairs = {pair.id: pair for pair in engine.pairs}
    snapshots: dict[str, dict[str, Any]] = {}
    for result in report.trade_pairs:
        pair = pairs[result.id]
        gross_r = pair_gross_r(result, pair, engine.params)
        snapshots[result.id] = {
            "structure_id": result.id,
            "session": result.session,
            "status": result.status,
            "outcome": (
                str(pair_outcome(result, pair, engine.params))
                if result.status == "closed"
                else "open"
            ),
            "same_bar_resolved": pair.same_bar_resolved,
            "gross_pips": _value(result.gross_pnl_pips),
            "net_pips": _value(result.net_pnl_pips),
            "gross_r": gross_r,
            "net_r": gross_r - pair_cost_r(result, pair, engine.params),
        }
    return snapshots


def _different(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if any(left[key] != right[key] for key in ("status", "outcome", "same_bar_resolved")):
        return True
    return any(
        abs(float(left[key]) - float(right[key])) > 1e-12
        for key in ("gross_pips", "net_pips", "gross_r", "net_r")
    )


def _changes(
    baseline: dict[str, dict[str, Any]], candidate: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for structure_id in sorted(set(baseline) | set(candidate)):
        before = baseline.get(structure_id)
        after = candidate.get(structure_id)
        if before is None:
            changes.append(
                {
                    "structure_id": structure_id,
                    "change_kind": "added_vs_tier_0",
                    "tier_0": None,
                    "tier": after,
                }
            )
        elif after is None:
            changes.append(
                {
                    "structure_id": structure_id,
                    "change_kind": "missing_vs_tier_0",
                    "tier_0": before,
                    "tier": None,
                }
            )
        elif _different(before, after):
            changes.append(
                {
                    "structure_id": structure_id,
                    "change_kind": "outcome_or_value_changed",
                    "tier_0": before,
                    "tier": after,
                    "delta_gross_pips": after["gross_pips"] - before["gross_pips"],
                    "delta_net_pips": after["net_pips"] - before["net_pips"],
                    "delta_gross_r": after["gross_r"] - before["gross_r"],
                    "delta_net_r": after["net_r"] - before["net_r"],
                }
            )
    return changes


def run_s5_resolver_bias(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> dict[str, Any]:
    """Run one immutable configuration through tiers 0-3; tier 4 is interface-only."""
    if not candles:
        raise ValueError("S5 requires at least one candle")

    available_m1 = m1_bars or []
    coverage = m1_coverage(candles, available_m1, params)
    use_subpath = coverage.status == "complete"
    tier_runs: list[dict[str, Any]] = []
    snapshots_by_tier: dict[int, dict[str, dict[str, Any]]] = {}
    baseline_totals: dict[str, float] | None = None

    shared = params.model_dump(mode="json")
    shared.pop("intrabar_mode", None)

    for tier, mode in S5_EXECUTABLE_TIERS:
        tier_params = EngineParams.model_validate(params.model_dump() | {"intrabar_mode": mode})
        for key, value in shared.items():
            if tier_params.model_dump(mode="json")[key] != value:
                raise ValueError(f"resolver tier changed shared configuration field {key}")
        subpath_bars = available_m1 if use_subpath and mode in {
            IntrabarMode.M1,
            IntrabarMode.M1_CONSERVATIVE,
        } else []
        engine = ClosedBarEngine(windows, tier_params, anchors, subpath_bars)
        engine.run(candles)
        backtest = engine.report(symbol, timeframe, source).model_copy(
            update={"bar_count": len(candles)}
        )
        completed = completed_structures(engine, backtest)
        metrics = shared_cell_metrics(engine, backtest, completed)
        snapshots = _structure_snapshots(engine, backtest)
        snapshots_by_tier[tier] = snapshots
        totals = {
            key: float(metrics[key]) for key in ("gross_pips", "net_pips", "gross_r", "net_r")
        }
        if baseline_totals is None:
            baseline_totals = totals
        fallback = None
        if mode in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE} and not use_subpath:
            fallback = S5_FALLBACK
        tier_runs.append(
            {
                "tier": tier,
                "intrabar_mode": mode.value,
                "status": "executed",
                "m1_subpath_used": bool(subpath_bars),
                "fallback": fallback,
                "completed_structures": int(metrics["completed_structures"]),
                "structures_total": len(snapshots),
                "same_bar_structures": sum(
                    snapshot["same_bar_resolved"] for snapshot in snapshots.values()
                ),
                "same_bar_resolution_rate": backtest.same_bar_resolution_rate,
                "same_bar_r": backtest.same_bar_r,
                **totals,
                "delta_vs_tier_0": {
                    key: totals[key] - baseline_totals[key] for key in baseline_totals
                },
                "changed_structures": [],
                "changed_structure_count": 0,
            }
        )

    baseline = snapshots_by_tier[0]
    for tier_run in tier_runs:
        changes = _changes(baseline, snapshots_by_tier[int(tier_run["tier"])])
        tier_run["changed_structures"] = changes
        tier_run["changed_structure_count"] = len(changes)

    tier_runs.append(
        {
            "tier": S5_TICK_TIER,
            "intrabar_mode": IntrabarMode.TICK.value,
            "status": "interface_only_unavailable",
            "reason": "TickResolver requires a bid/ask tick source; implementation is deferred.",
            "m1_subpath_used": False,
            "fallback": None,
            "completed_structures": None,
            "structures_total": None,
            "same_bar_structures": None,
            "same_bar_resolution_rate": None,
            "same_bar_r": None,
            "gross_pips": None,
            "net_pips": None,
            "gross_r": None,
            "net_r": None,
            "delta_vs_tier_0": {
                "gross_pips": None,
                "net_pips": None,
                "gross_r": None,
                "net_r": None,
            },
            "changed_structures": [],
            "changed_structure_count": None,
        }
    )

    return {
        "study": "s5_resolver_ladder_bias",
        "symbol": symbol,
        "timeframe": timeframe.value,
        "source": source,
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat(),
        "last_bar_ts": candles[-1].ts.isoformat(),
        "candle_set_sha256": candle_sha256(candles),
        "shared_params": shared,
        "m1_coverage": coverage.model_dump(mode="json"),
        "baseline_tier": 0,
        "executable_tier_count": len(S5_EXECUTABLE_TIERS),
        "tier_count_including_interface": len(S5_EXECUTABLE_TIERS) + 1,
        "export_calibration": {
            "status": "unverified",
            "fixtures_required": [
                "tests/fixtures/session-hedging-XAUUSD-M15.csv",
                "tests/fixtures/session-hedging-XAUUSD-H1.csv",
                "tests/fixtures/session-hedging-XAUUSD-H4.csv",
            ],
            "reference_same_bar_rates": {"M15": 0.106, "H1": 0.112, "H4": 0.051},
            "reason": "The M15/H1/H4 export CSVs are absent; no reference rate is inferred.",
        },
        "data_sufficiency": {
            "descriptive_only": True,
            "reason": "2,000 M15 bars cover roughly 30 days of one symbol.",
            "needed": "The named M15/H1/H4 exports for §0 calibration, plus contiguous M15 "
            "history with covering M1 or bid/ask ticks across varied regimes.",
        },
        "tiers": tier_runs,
    }


def render_s5_markdown(report: dict[str, Any]) -> str:
    coverage = report["m1_coverage"]
    lines = [
        "# S5 resolver ladder bias calibration",
        "",
        "One immutable configuration is run through every executable resolver tier. Tier 4 is "
        "reported as interface-only because no bid/ask tick source is implemented. All deltas "
        "are measured against tier 0 (`optimistic`).",
        "",
        "## Run identity",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol / timeframe | {report['symbol']} / {report['timeframe']} |",
        f"| Bars | {report['bar_count']} |",
        f"| Date bounds | {report['first_bar_ts']} to {report['last_bar_ts']} |",
        f"| Candle fingerprint | `{report['candle_set_sha256']}` |",
        f"| M1 coverage | {coverage['status']}: {coverage['covered_parent_bars']} / "
        f"{coverage['total_parent_bars']} ({coverage['covered_parent_fraction']:.2%}) |",
        f"| Uniform fallback | `{coverage['subpath_fallback'] or 'none'}` |",
        "",
        "Partial M1 chronology is never mixed into a window. When coverage is not complete, "
        f"tiers 2 and 3 use `{S5_FALLBACK}` for the full window.",
        "",
        "## Resolver totals and deltas",
        "",
    ]
    rows = []
    for tier in report["tiers"]:
        delta = tier["delta_vs_tier_0"]
        rows.append(
            [
                str(tier["tier"]),
                tier["intrabar_mode"],
                tier["status"],
                tier["fallback"] or "—",
                markdown.num(tier["completed_structures"]),
                markdown.num(tier["same_bar_resolution_rate"], 4),
                markdown.num(tier["same_bar_r"], 4),
                markdown.num(tier["gross_pips"]),
                markdown.num(tier["net_pips"]),
                markdown.num(tier["gross_r"], 4),
                markdown.num(tier["net_r"], 4),
                markdown.num(delta["gross_pips"]),
                markdown.num(delta["net_pips"]),
                markdown.num(delta["gross_r"], 4),
                markdown.num(delta["net_r"], 4),
                markdown.num(tier["changed_structure_count"]),
            ]
        )
    lines += markdown.table(
        [
            "Tier",
            "Mode",
            "Status",
            "Fallback",
            "Completed",
            "Same-bar rate",
            "Same-bar R",
            "Gross pips",
            "Net pips",
            "Gross R",
            "Net R",
            "Δ gross pips",
            "Δ net pips",
            "Δ gross R",
            "Δ net R",
            "Changed structures",
        ],
        rows,
        align_right_from=4,
    )
    lines += [
        "## Every changed structure",
        "",
        "A row appears when a structure is added, missing, changes classification, receives a "
        "different same-bar tag, or changes pip/R value versus tier 0. No changed structure is "
        "discarded.",
        "",
    ]
    change_rows: list[list[str]] = []
    for tier in report["tiers"]:
        for change in tier["changed_structures"]:
            before = change["tier_0"] or {}
            after = change["tier"] or {}
            change_rows.append(
                [
                    str(tier["tier"]),
                    change["structure_id"],
                    change["change_kind"],
                    str(before.get("outcome", "—")),
                    str(after.get("outcome", "—")),
                    markdown.num(before.get("gross_pips")),
                    markdown.num(after.get("gross_pips")),
                    markdown.num(before.get("gross_r"), 4),
                    markdown.num(after.get("gross_r"), 4),
                ]
            )
    if change_rows:
        lines += markdown.table(
            [
                "Tier",
                "Structure",
                "Change",
                "Tier 0 outcome",
                "Tier outcome",
                "Tier 0 gross pips",
                "Tier gross pips",
                "Tier 0 gross R",
                "Tier gross R",
            ],
            change_rows,
            align_right_from=5,
        )
    else:
        lines += ["No executable tier changed a structure on this window.", ""]
    lines += [
        "## Calibration status and limits",
        "",
        "The §0 same-bar comparison remains **unverified**: M15 10.6%, H1 11.2%, H4 5.1%. "
        "The named export CSVs are absent from `tests/fixtures`; no fixture data was invented, "
        "synthesized, or approximated. The export-dependent acceptance test remains an explicit "
        "skip.",
        "",
        "The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies this harness "
        "and provides descriptive calibration only; it cannot establish a resolver constant for "
        "other timeframes or regimes. That needs the named exports and contiguous M15 history "
        "with covering M1 or bid/ask ticks across varied regimes.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"
