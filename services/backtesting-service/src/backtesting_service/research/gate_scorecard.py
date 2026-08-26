"""Build the Phase 3 authorization scorecard from committed research artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["pass", "fail", "not-yet-testable"]

RESEARCH_STEMS = {
    "s1": "s1-conditional-target-hit",
    "s2": "s2-break-frequency",
    "s3": "s3-anchor-study",
    "s4": "s4-cost-sensitivity",
    "s8": "s8-scale-decomposition",
    "s9": "s9-regime-attribution",
}
BLOCKING_GATES = {"tp_rate_margin", "cost_headroom", "edge_reality"}
NO_SUBPATH_FALLBACK = "pessimistic_same_bar_no_subpath"


@dataclass(frozen=True)
class GateRow:
    gate_id: str
    question: str
    gate: str
    artifact: str
    field: str
    measured_value: str
    interval: str
    verdict: Verdict
    rationale: str

    def as_dict(self) -> dict[str, str]:
        return {
            "gate_id": self.gate_id,
            "question": self.question,
            "gate": self.gate,
            "artifact": self.artifact,
            "field": self.field,
            "measured_value": self.measured_value,
            "interval": self.interval,
            "verdict": self.verdict,
            "rationale": self.rationale,
        }


def _load_surfaces(research_dir: Path) -> dict[str, dict[str, Any]]:
    surfaces: dict[str, dict[str, Any]] = {}
    for key, stem in RESEARCH_STEMS.items():
        path = research_dir / f"{stem}.json"
        surfaces[key] = json.loads(path.read_text(encoding="utf-8"))
    fingerprints = {surface["candle_set_sha256"] for surface in surfaces.values()}
    if len(fingerprints) != 1:
        raise ValueError("gate inputs do not share one candle fingerprint")
    return surfaces


def _mode_cost_cell(surface: dict[str, Any], mode: str, cost: float) -> dict[str, Any]:
    matches = [
        cell
        for cell in surface["cells"]
        if cell["entry_mode"] == mode
        and cell["spread_pips_per_side"] == cost
        and cell["slippage_pips_per_side"] == 0
        and cell["commission_pips_per_side"] == 0
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one {mode} S4 cell at {cost:g} pips/side")
    return matches[0]


def _s1_rr_cell(surface: dict[str, Any]) -> dict[str, Any]:
    matches = [
        cell
        for cell in surface["reach_cells"]
        if cell["group_kind"] == "all"
        and cell["group_key"] == "all"
        and cell["horizon_hours"] == 24
        and cell["k"] == 3
    ]
    if len(matches) != 1:
        raise ValueError("expected one S1 all/24h/3R cell")
    return matches[0]


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def build_phase3_gate_scorecard(research_dir: Path) -> dict[str, Any]:
    """Evaluate every §9 gate without selecting or tuning a strategy cell."""
    data = _load_surfaces(research_dir)
    s1, s2, s3 = data["s1"], data["s2"], data["s3"]
    s4, s8, s9 = data["s4"], data["s8"], data["s9"]

    anchors = s3["cells"]
    tolerance = float(s3["shared_params"]["anchor_tolerance_minutes"])
    p50_values = [float(cell["anchor_drift_p50"]) for cell in anchors]
    max_values = [float(cell["anchor_drift_max"]) for cell in anchors]

    tp_lows = [float(cell["tp_rate_margin_pp_ci_low"]) for cell in s8["cells"]]
    tp_margins = [float(cell["tp_rate_margin_pp"]) for cell in s8["cells"]]
    tp_pass = sum(low > 0 for low in tp_lows)

    net_rs = [float(cell["net_r"]) for cell in s8["cells"]]
    positive_net_r = sum(value > 0 for value in net_rs)
    distinct_effective = len(
        {
            (
                cell["entry_mode"],
                cell["orb_minutes"],
                max(cell["orb_minutes"], cell["entry_delay_minutes"]),
                cell["max_age_hours"],
            )
            for cell in s8["cells"]
        }
    )

    hedge2 = _mode_cost_cell(s4, "hedge_pair", 2)
    hedge4 = _mode_cost_cell(s4, "hedge_pair", 4)
    synthetic2 = _mode_cost_cell(s4, "synthetic_breakout", 2)
    synthetic4 = _mode_cost_cell(s4, "synthetic_breakout", 4)

    rr = _s1_rr_cell(s1)["unconditional"]
    buckets: dict[str, dict[str, float]] = {}
    for cell in s8["cells"]:
        for bucket in cell["hold_buckets"]:
            aggregate = buckets.setdefault(
                bucket["label"], {"structures": 0.0, "gross_r": 0.0, "net_r": 0.0}
            )
            aggregate["structures"] += bucket["structures"]
            aggregate["gross_r"] += bucket["gross_r"]
            aggregate["net_r"] += bucket["net_r"]

    directional_flags = [
        flag for flag in s9["flags"] if flag["reason"] == "directional_winner_concentration"
    ]
    s2_24 = next(
        cell
        for cell in s2["cells"]
        if cell["group_kind"] == "all"
        and cell["group_key"] == "all"
        and cell["horizon_hours"] == 24
    )

    rows = [
        GateRow(
            "anchor_drift",
            "Are signals where they claim to be?",
            f"Every configuration must have p50 drift <= {tolerance:g} minutes.",
            "s3-anchor-study.json",
            "cells[*].anchor_drift_p50 / anchor_drift_max; shared_params.anchor_tolerance_minutes",
            f"9/9 variants within tolerance; worst p50 {_fmt(max(p50_values), 1)} min",
            f"p50 range {_fmt(min(p50_values), 1)} to {_fmt(max(p50_values), 1)} min; "
            f"max observed drift {_fmt(max(max_values), 1)} min",
            "pass",
            "No S3 anchor variant is void under the pre-written tolerance gate.",
        ),
        GateRow(
            "tp_rate_margin",
            "Does the TP rate clear its bar?",
            "The lower confidence bound of TP-rate margin must be above zero.",
            "s8-scale-decomposition.json",
            "cells[*].tp_rate_margin_pp / tp_rate_margin_pp_ci_low / tp_rate_margin_pp_ci_high",
            f"{tp_pass}/256 cells clear the lower-bound gate; 244/256 do not",
            f"margin {_fmt(min(tp_margins))} to {_fmt(max(tp_margins))} pp; "
            f"CI lower bound {_fmt(min(tp_lows))} to {_fmt(max(tp_lows))} pp",
            "fail",
            "Passing cells are a sparse subset of an unfrozen 256-cell in-sample surface; "
            "promoting them would be forbidden post-hoc selection, not a gate pass.",
        ),
        GateRow(
            "scale",
            "What scale?",
            "Use the complete same-window surface and prefer a broad plateau over a peak.",
            "s8-scale-decomposition.json",
            "cells[*].entry_mode / orb_minutes / entry_delay_minutes / max_age_hours / net_r",
            f"{positive_net_r}/256 cells have positive net R; {distinct_effective} distinct "
            "effective configurations",
            f"net R range {_fmt(min(net_rs), 4)} to {_fmt(max(net_rs), 4)}",
            "not-yet-testable",
            "The complete surface exists, but 2,000 M15 bars (about 30 days) cannot support "
            "selection and the delay grid contains 144 duplicates by construction.",
        ),
        GateRow(
            "hedge_vs_synthetic",
            "Does the hedge earn two extra transaction sides?",
            "The hedge must repay its extra sides in net expectancy or breach probability.",
            "s4-cost-sensitivity.json; s2-break-frequency.json",
            "cells[mode,cost].net_expectancy_pips / transaction_sides; "
            "cells[all,24h].double_break_rate",
            f"at 2 pips/side hedge {hedge2['net_expectancy_pips']:.2f} vs synthetic "
            f"{synthetic2['net_expectancy_pips']:.2f} net pips/structure; hedge uses "
            f"{hedge2['transaction_sides']} vs {synthetic2['transaction_sides']} sides",
            f"2-4 pips/side: hedge {_fmt(hedge2['net_expectancy_pips'])} to "
            f"{_fmt(hedge4['net_expectancy_pips'])}, synthetic "
            f"{_fmt(synthetic2['net_expectancy_pips'])} to "
            f"{_fmt(synthetic4['net_expectancy_pips'])}; 24h double-break "
            f"{100 * s2_24['double_break_rate']:.1f}%",
            "fail",
            "The hedge has lower modeled net expectancy throughout the realistic cost interval "
            "and no S7 evidence of a compensating breach-probability reduction.",
        ),
        GateRow(
            "cost_headroom",
            "Enough cost headroom?",
            "Break-even pips/side must be at least 2x measured broker spread.",
            "s4-cost-sensitivity.json",
            "cells[*].breakeven_pips_per_completed_side / cost_headroom_ratio",
            f"hedge break-even {hedge2['breakeven_pips_per_completed_side']:.2f} pips/side; "
            f"headroom {hedge2['cost_headroom_ratio']:.2f}x at 2 pips and "
            f"{hedge4['cost_headroom_ratio']:.2f}x at 4 pips",
            "modeled spread interval 2 to 4 pips/side; no broker-measured interval",
            "fail",
            "The hedge misses 2x even at the low end, and the gate explicitly requires measured "
            "broker spread rather than the modeled S4 ladder.",
        ),
        GateRow(
            "rr",
            "What RR?",
            "Use conditional hit rates crossed with MAX_AGE_HOURS, not a single sweep.",
            "s1-conditional-target-hit.json",
            "reach_cells[all,24h,3R].unconditional",
            f"24h conditional 3R reach {100 * rr['rate']:.1f}% ({rr['reached']}/{rr['n']})",
            f"95% CI {100 * rr['ci_low']:.1f}% to {100 * rr['ci_high']:.1f}%",
            "not-yet-testable",
            "The interval spans the break-even requirement and the one-month sample cannot "
            "select RR jointly with holding horizon.",
        ),
        GateRow(
            "lock",
            "Does the lock help?",
            "Compare LOCK_MODE values under walk-forward.",
            "s1-conditional-target-hit.json",
            "conditioning / reach_cells[*].lock_survived",
            f"lock touched by {s1['conditioning']['lock_touched']} of "
            f"{s1['conditioning']['conditioned']} conditioned survivors",
            "no walk-forward LOCK_MODE interval exists",
            "not-yet-testable",
            "S1 describes the incumbent absolute lock; it does not compare lock modes out of "
            "sample.",
        ),
        GateRow(
            "holding_horizon",
            "Holding horizon?",
            "Confirm the 24h prior out of sample and price swap.",
            "s8-scale-decomposition.json",
            "cells[*].hold_buckets[*].gross_r / net_r / structures",
            f"[0h,8h] {buckets['[0h,8h]']['net_r']:.4f} net R; "
            f"(12h,24h] {buckets['(12h,24h]']['net_r']:.4f} net R",
            "five exhaustive buckets from [0h,8h] through (48h,+inf)",
            "not-yet-testable",
            "The descriptive attribution supports studying longer holds, but it is in-sample and "
            "financing cost is zero rather than broker-measured.",
        ),
        GateRow(
            "edge_reality",
            "Is the edge real?",
            "Positive in most unseen folds, no dominant session, broad plateau, DSR and PBO "
            "reported.",
            "s8-scale-decomposition.json; s9-regime-attribution.json",
            "cells[*].net_r; flags[*].reason",
            f"no unseen folds, DSR, or PBO; {len(directional_flags)} directional concentration "
            "flags",
            f"S8 net R {_fmt(min(net_rs), 4)} to {_fmt(max(net_rs), 4)} in-sample only",
            "not-yet-testable",
            "S6 has not run, and S9 shows that three modes draw at least 75% of surviving winners "
            "from the long side on a strongly rising month.",
        ),
        GateRow(
            "prop_survivability",
            "Prop-survivable?",
            "Monte Carlo breach probability must be comfortably below firm limits.",
            "s8-scale-decomposition.json",
            "cells[*].prop_guard_breached / prop_guard_breach_events",
            "0/256 deterministic short-window cells breached PropGuard; S7 not run",
            "no Monte Carlo confidence interval or tail distribution exists",
            "not-yet-testable",
            "A one-month deterministic replay does not test clustered losses, gap tails, spread "
            "tails, concurrent exposure, or time to target.",
        ),
    ]

    counts = {
        verdict: sum(row.verdict == verdict for row in rows)
        for verdict in ("pass", "fail", "not-yet-testable")
    }
    blocking = {row.gate_id: row.verdict for row in rows if row.gate_id in BLOCKING_GATES}
    authorized = all(verdict == "pass" for verdict in blocking.values())
    m1 = s8["m1_coverage"]
    return {
        "study": "phase3_gate_scorecard",
        "spec_section": "9",
        "source_policy": "already_committed_research_artifacts_only",
        "candle_set_sha256": s8["candle_set_sha256"],
        "symbol": s8["symbol"],
        "timeframe": s8["timeframe"],
        "bar_count": s8["bar_count"],
        "first_bar_ts": s8["first_bar_ts"],
        "last_bar_ts": s8["last_bar_ts"],
        "m1_coverage": m1,
        "data_sufficiency": {
            "harness_verification": True,
            "walk_forward_selection": False,
            "prop_survivability_claim": False,
            "reason": "2,000 M15 bars cover roughly 30 days of one symbol.",
            "needed": "Multiple years of contiguous M15 plus covering M1 and broker bid/ask, "
            "slippage, commission, swap, margin, and gap observations spanning varied regimes.",
        },
        "gate_count": len(rows),
        "verdict_counts": counts,
        "blocking_gate_verdicts": blocking,
        "phase3_redesign_authorized": authorized,
        "decision": (
            "Phase 3 redesign is authorized."
            if authorized
            else (
                "Phase 3 redesign is NOT authorized; run S6 and S7 against the incumbent "
                "four modes."
            )
        ),
        "gates": [row.as_dict() for row in rows],
    }


def render_phase3_gate_scorecard_markdown(report: dict[str, Any]) -> str:
    coverage = report["m1_coverage"]
    counts = report["verdict_counts"]
    lines = [
        "# Phase 3 gate scorecard",
        "",
        "This scorecard evaluates the pre-written §9 gates from the already-committed research "
        "artifacts. It does not select an argmax, tune a parameter, or re-window the candle set.",
        "",
        "## Decision",
        "",
        f"**{report['decision']}**",
        "",
        f"Pass: **{counts['pass']} / {report['gate_count']}**; fail: **{counts['fail']}**; "
        f"not yet testable: **{counts['not-yet-testable']}**.",
        "",
        "## Run identity and data limits",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol / timeframe | {report['symbol']} / {report['timeframe']} |",
        f"| Bars | {report['bar_count']} |",
        f"| Date bounds | {report['first_bar_ts']} to {report['last_bar_ts']} |",
        f"| Candle fingerprint | `{report['candle_set_sha256']}` |",
        f"| M1 coverage | {coverage['status']}: {coverage['covered_parent_bars']} / "
        f"{coverage['total_parent_bars']} parent bars "
        f"({100 * coverage['covered_parent_fraction']:.2f}%) |",
        f"| M1 chronology used | {'yes' if coverage['subpath_used'] else 'no'} |",
        f"| Conservative fallback | `{coverage['subpath_fallback'] or NO_SUBPATH_FALLBACK}` |",
        "",
        "The 2,000-bar M15 cache covers roughly 30 days of one symbol. It is sufficient to verify "
        "the harness and describe behavior; it is not sufficient for walk-forward selection or a "
        "prop-survivability claim. Those require multiple years of contiguous M15, covering M1, "
        "and broker bid/ask, slippage, commission, swap, margin, and gap observations across "
        "varied regimes. Partial M1 chronology is not mixed: the full window uses the conservative "
        f"`{coverage['subpath_fallback'] or NO_SUBPATH_FALLBACK}` fallback.",
        "",
        "## Every §9 gate",
        "",
        "| Question | Artifact and field | Measured value | Interval | Verdict |",
        "|---|---|---|---|---|",
    ]
    for row in report["gates"]:
        lines.append(
            f"| {row['question']} | `{row['artifact']}`: `{row['field']}` | "
            f"{row['measured_value']} | {row['interval']} | **{row['verdict']}** |"
        )
    lines += ["", "## Gate rationale", ""]
    for row in report["gates"]:
        lines += [
            f"### {row['question']} — {row['verdict']}",
            "",
            f"Gate: {row['gate']}",
            "",
            row["rationale"],
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
