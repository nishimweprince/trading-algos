"""The harness that decides: the plan's §1.7 gates, on the held-out period, once.

The selected candidate and policy run on the held-out period with the final
model's scores, through the same shadow backtest:

| run | queue model | latency | scores |
|---|---|---|---|
| base | queue | 1x | final, held out |
| pessimistic | pessimistic | 1x | final, held out |
| latency_2x | queue | 2x | final, held out |
| stress | queue | 1x | OOF, the most volatile validation days |

Every gate is written with its value and threshold; ``passed`` is all of them.
The model directory is written either way (``load_model`` refuses one that
failed), with ``strategy.md`` on a pass and ``REPORT.md`` on a fail. The
held-out ledger is written before anything is evaluated, so a crash still
counts as the one look.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ofi_scalper_service.model import write_model

from .backtest import DayJob, Latency, VariantSpec, aggregate, run_days
from .cv import Split, period_of, record_holdout_use
from .train import Candidate, score_final

__all__ = ["THRESHOLDS", "evaluate", "gate_table"]

THRESHOLDS = {
    "sharpe": 1.5,
    "max_drawdown_pct": 15.0,
    "hit_rate": 0.55,
    "t_stat": 2.0,
    "net_edge_bp": 0.0,
    "min_trades": 30,
    "stress_loss_tolerance": 0.2,  # a day may overshoot the daily loss limit by 20% (slippage)
    "stress_days": 3,
}


def _gate(value: Any, threshold: Any, ok: bool, note: str = "") -> dict[str, Any]:
    out = {"value": value, "threshold": threshold, "passed": bool(ok)}
    if note:
        out["note"] = note
    return out


def _gt(value: float | None, threshold: float) -> bool:
    return value is not None and value > threshold


def gate_table(
    agg: dict[str, dict[str, Any]],
    *,
    split: Split,
    selection: dict[str, Any],
    daily_loss_limit_usd: float,
) -> dict[str, dict[str, Any]]:
    base, summary = agg["base"], agg["base"]["summary"]
    stats = base["stats"]
    sub = "day" if split.period == "week" else "week"
    sub_periods: dict[str, float] = {}
    for day, net in base["daily_net_usd"].items():
        key = day if sub == "day" else period_of(day, "week")
        sub_periods[key] = sub_periods.get(key, 0.0) + net
    positive_sub = sum(1 for v in sub_periods.values() if v > 0)
    chosen = selection["chosen"] or {}
    validation = chosen.get("by_period_net_usd", {})
    positive_validation = sum(1 for v in validation.values() if v > 0)
    by_symbol = base["by_symbol_net_usd"]
    stress = agg.get("stress")
    worst = stress["stats"]["worst_day_usd"] if stress else None
    loss_cap = -daily_loss_limit_usd * (1 + THRESHOLDS["stress_loss_tolerance"])
    faults = [h for a in agg.values() for h in a["halts"] if h.get("reason") == "execution_fault"]
    return {
        "min_trades": _gate(
            summary["trades"],
            THRESHOLDS["min_trades"],
            summary["trades"] >= THRESHOLDS["min_trades"],
        ),
        "sharpe": _gate(stats["sharpe"], THRESHOLDS["sharpe"], _gt(stats["sharpe"], 1.5)),
        "max_drawdown_pct": _gate(
            stats["max_drawdown_pct"],
            THRESHOLDS["max_drawdown_pct"],
            stats["max_drawdown_pct"] < THRESHOLDS["max_drawdown_pct"],
        ),
        "hit_rate": _gate(
            summary["win_rate"], THRESHOLDS["hit_rate"], _gt(summary["win_rate"], 0.55)
        ),
        "t_stat": _gate(summary["t_stat"], THRESHOLDS["t_stat"], _gt(summary["t_stat"], 2.0)),
        "net_edge_bp": _gate(
            summary["mean_net_bp"], THRESHOLDS["net_edge_bp"], _gt(summary["mean_net_bp"], 0.0)
        ),
        "latency_2x_profitable": _gate(
            agg["latency_2x"]["stats"]["net_usd"], 0.0, agg["latency_2x"]["stats"]["net_usd"] > 0
        ),
        "pessimistic_queue_profitable": _gate(
            agg["pessimistic"]["stats"]["net_usd"], 0.0, agg["pessimistic"]["stats"]["net_usd"] > 0
        ),
        "stable_held_out": _gate(
            f"{positive_sub}/{len(sub_periods)} {sub}s positive",
            "majority",
            positive_sub * 2 > len(sub_periods),
        ),
        "stable_validation": _gate(
            f"{positive_validation}/{len(validation)} {split.period}s positive",
            "majority",
            positive_validation * 2 > len(validation),
        ),
        "btc_positive": _gate(by_symbol.get("BTCUSDT"), 0.0, _gt(by_symbol.get("BTCUSDT"), 0.0)),
        "eth_positive": _gate(by_symbol.get("ETHUSDT"), 0.0, _gt(by_symbol.get("ETHUSDT"), 0.0)),
        "stress_days_within_limits": _gate(
            worst,
            round(loss_cap, 4),
            stress is not None and (worst is None or worst >= loss_cap) and not faults,
            note=f"execution-fault halts: {len(faults)}",
        ),
    }


def _stress_days(features_dir: Path, days: list[str], count: int) -> list[str]:
    """The most volatile days (median rv_60s_bp) among those given."""
    ranked = []
    for day in days:
        path = features_dir / f"{day}.parquet"
        if path.is_file():
            rv = pd.read_parquet(path, columns=["rv_60s_bp"])["rv_60s_bp"].median()
            ranked.append((float(rv) if pd.notna(rv) else 0.0, day))
    return [day for _, day in sorted(ranked, reverse=True)[:count]]


def _strategy_md(version: str, candidate: Candidate, policy: dict[str, Any], gates: dict) -> str:
    return f"""# Strategy {version}

Generated by research/gates.py after the held-out evaluation passed. Code
decides every threshold, size, veto and order; the model only advises.

- **Model**: CatBoost multiclass (down / none / up), horizon {policy["horizon_s"]} s,
  isotonic calibration fit on out-of-fold predictions.
- **Features** ({len(candidate.features)}): {", ".join(candidate.features)}.
- **Labels**: triple barrier on mid at +/-{policy["barrier_bp"]} bp within {policy["horizon_s"]} s.
- **Entry**: calibrated p >= {policy["threshold"]} x the gate's threshold multiplier, and
  (p - p_opposite) x {policy["barrier_bp"]} bp >= 2 x maker fee + {policy["buffer_bp"]} bp.
  Post-only limit at the touch; cancelled after {policy["entry_timeout_ms"]} ms or when p
  falls below the threshold or the symbol is blocked.
- **Take profit**: reduce-only post-only limit {policy["barrier_bp"]} bp from the fill.
- **Stop**: reduce-only market when the mid crosses {policy["barrier_bp"]} bp against the fill.
- **Time stop**: reduce-only market after {policy["horizon_s"] * policy["time_stop_mult"]} s.
- **No-trade windows**: the regime gate (funding blackout, liquidation burst,
  volatility, wide spread), stale books, depth resyncs, stream outages, halts.
- **Invalidation**: stop and re-research if live or shadow net edge per trade is
  <= 0 bp over 100 trades, or the Brier score on our own fills is worse than the
  cross-validated one ({gates.get("cv_brier")}), or any hard-risk halt fires twice in a week.
"""


def _report_md(version: str, split: Split, gates: dict[str, dict[str, Any]], agg: dict) -> str:
    failed = [(name, g) for name, g in gates.items() if not g["passed"]]
    lines = [
        f"# {version}: does not clear the gates",
        "",
        f"Run `{split.run}` ({split.period}ly), held out {', '.join(split.holdout)}, evaluated "
        f"{datetime.now(UTC).date()}. Per ofi-scalper-plan.md §1.7 this is a valid result: "
        "do not tune on the held-out period and do not proceed to shadow with this model.",
        "",
        "## Failed",
        "",
        "| gate | value | threshold |",
        "|---|---|---|",
        *[f"| {name} | {g['value']} | {g['threshold']} |" for name, g in failed],
        "",
        "## Held-out results",
        "",
        "| run | trades | net USD | mean net bp | win rate |",
        "|---|---|---|---|---|",
        *[
            f"| {name} | {a['summary']['trades']} | {a['stats']['net_usd']} | "
            f"{a['summary']['mean_net_bp']} | {a['summary']['win_rate']} |"
            for name, a in agg.items()
        ],
        "",
        "Next: more recorded data and a new run, or (per the plan) larger-tick altcoin perps.",
    ]
    return "\n".join(lines) + "\n"


def evaluate(
    split: Split,
    selection: dict[str, Any],
    *,
    record_dir: Path,
    features_dir: Path,
    research_dir: Path,
    models_dir: Path,
    common: dict[str, Any],
    latency: Latency,
    daily_loss_limit_usd: float,
    n_jobs: int = 1,
    version: str | None = None,
) -> dict[str, Any]:
    chosen = selection.get("chosen")
    if chosen is None:
        report = (
            f"# {split.run}: no candidate to evaluate\n\nNo horizon/threshold reached "
            "the minimum trade count on the validation periods, so the held-out period "
            "was not touched.\n"
        )
        (research_dir / "REPORT.md").write_text(report)
        return {"passed": False, "evaluated": False, "report": str(research_dir / "REPORT.md")}
    candidate = Candidate.load(Path(chosen["candidate"]))
    version = version or f"{split.run}-h{candidate.horizon_s:g}-{datetime.now(UTC):%Y%m%d%H%M}"
    record_holdout_use(research_dir / "holdout_ledger.json", split, version)

    held_out = split.days_of(split.holdout)
    score_final(candidate, features_dir=features_dir, days=held_out)
    threshold = chosen["threshold"]
    runs = (
        VariantSpec("base", str(candidate.path), "final", threshold, "queue", 1.0),
        VariantSpec("pessimistic", str(candidate.path), "final", threshold, "pessimistic", 1.0),
        VariantSpec("latency_2x", str(candidate.path), "final", threshold, "queue", 2.0),
    )
    jobs = [DayJob(day, str(record_dir), runs, latency, **common) for day in held_out]
    stress_days = _stress_days(
        features_dir, split.days_of(split.walk_forward[1:]), THRESHOLDS["stress_days"]
    )
    stress = (VariantSpec("stress", str(candidate.path), "oof", threshold, "queue", 1.0),)
    jobs += [DayJob(day, str(record_dir), stress, latency, **common) for day in stress_days]
    agg = aggregate(run_days(jobs, n_jobs), capital_usd=common["shadow_equity_usd"])
    gates = gate_table(
        agg, split=split, selection=selection, daily_loss_limit_usd=daily_loss_limit_usd
    )
    passed = all(g["passed"] for g in gates.values())
    cv = json.loads((candidate.path / "cv_report.json").read_text())
    policy = {
        "horizon_s": candidate.horizon_s,
        "threshold": threshold,
        "barrier_bp": candidate.barrier_bp,
        "buffer_bp": 0.0,
        "entry_timeout_ms": 1000,
        "time_stop_mult": 2.0,
    }
    verdict = {
        "passed": passed,
        "binding": split.binding,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "gates": gates,
        "thresholds": THRESHOLDS,
        "run": {k: v for k, v in split.as_dict().items() if k != "days"},
        "held_out_days": held_out,
        "stress_days": stress_days,
        "latency": {"feed_ms": latency.feed_ms, "order_ms": latency.order_ms},
        "fees_bp": {"maker": common["maker_bp"], "taker": common["taker_bp"]},
        "selection": chosen,
        "cv_brier": {k: v["brier"] for k, v in cv["calibrated"].items()},
        "data": candidate.meta.get("data", {}),
    }
    summary = {name: {k: v for k, v in a.items() if k != "trades"} for name, a in agg.items()}
    extra = {
        "cv_report.json": (candidate.path / "cv_report.json").read_text(),
        "shap_summary.json": (candidate.path / "shap_summary.json").read_text(),
        "selection.json": json.dumps(selection, indent=2),
        "held_out_summary.json": json.dumps(summary, indent=2, default=str),
    }
    if passed:
        extra["strategy.md"] = _strategy_md(version, candidate, policy, verdict)
    path = write_model(
        models_dir,
        version,
        model_file=candidate.path / "model.cbm",
        features=candidate.features,
        policy=policy,
        calibrator=json.loads((candidate.path / "calibrator.json").read_text()),
        engine=candidate.engine(),
        gates=verdict,
        extra_files=extra,
        data=candidate.meta.get("data", {}),
    )
    out: dict[str, Any] = {
        "passed": passed,
        "evaluated": True,
        "version": version,
        "model": str(path),
    }
    if not passed:
        report = research_dir / "REPORT.md"
        report.write_text(_report_md(version, split, gates, agg))
        out["report"] = str(report)
    return out
