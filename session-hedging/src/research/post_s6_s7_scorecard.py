"""Post-S6/S7 §9 scorecard. Does not overwrite the original blocking scorecard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.gate_scorecard import (
    BLOCKING_GATES,
    NO_SUBPATH_FALLBACK,
    GateRow,
    build_phase3_gate_scorecard,
    render_phase3_gate_scorecard_markdown,
)

ORIGINAL_SCORECARD_STEM = "phase3-gate-scorecard"
POST_SCORECARD_STEM = "phase3-post-s6-s7-scorecard"


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _pct(value: float) -> str:
    return f"{100 * value:.4f}%"


def build_post_s6_s7_scorecard(research_dir: Path) -> dict[str, Any]:
    """Refresh edge and prop rows from S6/S7 without rewriting the original scorecard."""
    base = build_phase3_gate_scorecard(research_dir)
    s6 = json.loads((research_dir / "s6-nested-walk-forward.json").read_text(encoding="utf-8"))
    s7 = json.loads((research_dir / "s7-propguard-monte-carlo.json").read_text(encoding="utf-8"))

    aggregate = s6["aggregate_unseen"]
    dsr = s6["deflated_sharpe_ratio"]
    cscv = s6["cscv"]
    folds = s6["folds"]
    positive_expectancy = sum(fold["unseen_net_expectancy_r"] > 0 for fold in folds)
    positive_final_r = sum(fold["unseen_net_r"] > 0 for fold in folds)

    s7_clusters = [mode["cluster_count"] for mode in s7["modes"]]
    s7_daily = [
        mode["simulation"]["daily_limit_breaches"]["3"]["breach_probability"]
        for mode in s7["modes"]
    ]
    s7_free = [
        mode["simulation"]["minimum_free_margin_pct_distribution"]["p01"] for mode in s7["modes"]
    ]

    replacements = {
        "edge_reality": GateRow(
            "edge_reality",
            "Is the edge real?",
            "Positive in most unseen folds, no dominant session, broad plateau, DSR and PBO "
            "reported.",
            "s6-nested-walk-forward.json",
            "aggregate_unseen.net_r / net_expectancy_r; folds[*].unseen_net_r; "
            "deflated_sharpe_ratio.probability; cscv.probability_of_backtest_overfitting",
            (
                f"unseen aggregate {_fmt(aggregate['net_pips'])} net pips / "
                f"{_fmt(aggregate['net_r'], 4)} net R over {aggregate['completed_structures']} "
                f"structures; {positive_expectancy}/4 folds positive expectancy; "
                f"{positive_final_r}/4 folds positive marked R"
            ),
            (
                f"DSR probability {_pct(dsr['probability'])}; PBO "
                f"{_pct(cscv['probability_of_backtest_overfitting'])}; "
                f"raw Sharpe {_fmt(dsr['sharpe'], 4)}"
            ),
            "fail",
            "S6 supplies failed descriptive out-of-sample evidence: the rolling unseen aggregate "
            "is negative, most folds are not positive, and DSR/PBO are reported. This does not "
            "authorize redesign promotion; it changes edge reality from not-yet-testable to fail.",
        ),
        "prop_survivability": GateRow(
            "prop_survivability",
            "Prop-survivable?",
            "Monte Carlo breach probability must be comfortably below firm limits.",
            "s7-propguard-monte-carlo.json",
            "modes[*].simulation.daily_limit_breaches / total_limit_breaches / "
            "minimum_free_margin_pct_distribution",
            (
                f"0/8000 paths breached requested 3%/5% daily or 6%/10% total limits; "
                f"cluster counts {min(s7_clusters)} to {max(s7_clusters)}; "
                f"1st-percentile free-margin proxy {_fmt(min(s7_free))} to {_fmt(max(s7_free))}%"
            ),
            (
                f"daily 3% breach probability {min(s7_daily):.2%} to {max(s7_daily):.2%}; "
                "conditional times undefined because no path breached"
            ),
            "not-yet-testable",
            "S7 is a complete descriptive harness, not a survivability claim: libraries contain "
            "only 4 to 15 clusters per mode, M1 is partial, costs and tails are modeled, sizing "
            "is conservative 0.1% equity per R, and free margin is a risk-budget proxy rather "
            "than broker margin. Multi-year clusters and broker execution/margin observations "
            "are still required.",
        ),
    }

    rows = []
    for row in base["gates"]:
        replacement = replacements.get(row["gate_id"])
        rows.append(replacement.as_dict() if replacement is not None else row)

    counts = {
        verdict: sum(row["verdict"] == verdict for row in rows)
        for verdict in ("pass", "fail", "not-yet-testable")
    }
    blocking = {row["gate_id"]: row["verdict"] for row in rows if row["gate_id"] in BLOCKING_GATES}
    return {
        **base,
        "study": "phase3_post_s6_s7_scorecard",
        "source_policy": "already_committed_research_artifacts_only_including_s6_s7",
        "original_scorecard": f"{ORIGINAL_SCORECARD_STEM}.json",
        "overwrites_original_scorecard": False,
        "phase3_redesign_authorized": False,
        "gate_count": len(rows),
        "verdict_counts": counts,
        "blocking_gate_verdicts": blocking,
        "decision": (
            "Phase 3 redesign remains NOT authorized. Edge reality is failed descriptive "
            "evidence from S6; S7 remains insufficient for a prop-survivability claim. The "
            "original pre-redesign blocking scorecard is unchanged."
        ),
        "gates": rows,
        "s6_s7_caveats": {
            "edge_reality": "failed_descriptive_evidence",
            "prop_survivability": "descriptive_inconclusive",
            "s6_unseen_net_r": aggregate["net_r"],
            "s6_unseen_net_pips": aggregate["net_pips"],
            "s6_dsr_probability": dsr["probability"],
            "s6_pbo": cscv["probability_of_backtest_overfitting"],
            "s7_seed": s7["seed"],
            "s7_simulations_per_mode": s7["simulation_count_per_mode"],
        },
    }


def render_post_s6_s7_scorecard_markdown(report: dict[str, Any]) -> str:
    body = render_phase3_gate_scorecard_markdown(report)
    coverage = report["m1_coverage"]
    header = [
        "# Phase 3 post-S6/S7 scorecard",
        "",
        "This scorecard incorporates S6's failed unseen-fold evidence and S7's descriptive "
        "PropGuard simulation. It does **not** overwrite "
        f"`{report['original_scorecard']}`, select a production coordinate, or authorize "
        "paper/live trading.",
        "",
        "## Decision",
        "",
        f"**{report['decision']}**",
        "",
        (
            f"Pass: **{report['verdict_counts']['pass']} / {report['gate_count']}**; "
            f"fail: **{report['verdict_counts']['fail']}**; "
            f"not yet testable: **{report['verdict_counts']['not-yet-testable']}**."
        ),
        "",
        "Edge reality is **fail** on descriptive S6 evidence. Prop-survivability remains "
        "**not-yet-testable** because S7 cannot support a claim: 4–15 clusters per mode, "
        "partial M1, modeled costs/tails, conservative sizing, and a free-margin proxy.",
        "",
        "## Run identity and data limits",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Original scorecard | `{report['original_scorecard']}` |",
        f"| Overwrites original | {str(report['overwrites_original_scorecard']).lower()} |",
        f"| Symbol / timeframe | {report['symbol']} / {report['timeframe']} |",
        f"| Bars | {report['bar_count']} |",
        f"| Candle fingerprint | `{report['candle_set_sha256']}` |",
        f"| Conservative fallback | `{coverage['subpath_fallback'] or NO_SUBPATH_FALLBACK}` |",
        "",
    ]
    # Reuse the original renderer for the gate table, then replace its title/decision.
    rest = body.split("## Every §9 gate", 1)[1]
    return "\n".join(header).rstrip() + "\n\n## Every §9 gate" + rest
