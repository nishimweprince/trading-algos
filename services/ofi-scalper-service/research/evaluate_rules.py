"""Gate a rule model the way a trained one is gated: ``python -m research.evaluate_rules``.

A rule model (``rules.py``) has no training data, so nothing ever judged it: it
traded on demo ungated. This replays it over recorded days through the shadow
bridge (queue fills at 1x latency, as the trained models' gates), with the
pessimistic-queue and 2x-latency variants and any ``--arm`` policy variants in
the same pass, and applies the gates that do not need a training split::

    python -m research.evaluate_rules --profile dev --version rules-v6 \\
        --from 2026-10-12 --to 2026-10-18 --arm guard3:guard_z=3,guard_tfi=0.3

Report: ``<OFI_RESEARCH_DIR>/rules/<version>/<from>_<to>/`` (``evaluation.json``,
``REPORT.md``). ``--write-gates`` also writes the verdict into the model as
``gates.json`` (once: a verdict is never replaced), after which a passing rule
model loads without ``OFI_ALLOW_UNGATED``.

Days used to choose a rule's parameters (``--arm`` sweeps) are in-sample for
it: write the gates from later days only.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .backtest import (
    DayJob,
    Latency,
    VariantSpec,
    aggregate,
    compare_arms,
    days_between,
    job_settings,
    parse_arm,
    run_days,
)
from .gates import SHALLOW_THRESHOLDS, THRESHOLDS, _gate, _gt, passed_all, signal_edge_gate
from .pipeline import eligible_days

__all__ = ["main", "rules_gate_table", "write_gates"]

# Below a week of days the shallow bars apply, as for a shallow trained run.
FULL_THRESHOLD_DAYS = 7


def rules_gate_table(
    agg: dict[str, dict[str, Any]], *, days: int, horizon_s: float, cost_bp: float
) -> dict[str, dict[str, Any]]:
    th = SHALLOW_THRESHOLDS if days < FULL_THRESHOLD_DAYS else THRESHOLDS
    base = agg["base"]
    summary, stats = base["summary"], base["stats"]
    return {
        "min_trades": _gate(
            summary["trades"], th["min_trades"], summary["trades"] >= th["min_trades"]
        ),
        "net_edge_bp": _gate(
            summary["mean_net_bp"],
            th["net_edge_bp"],
            _gt(summary["mean_net_bp"], th["net_edge_bp"]),
        ),
        "t_stat": _gate(summary["t_stat"], th["t_stat"], _gt(summary["t_stat"], th["t_stat"])),
        "hit_rate": _gate(
            summary["win_rate"], th["hit_rate"], _gt(summary["win_rate"], th["hit_rate"])
        ),
        "max_drawdown_pct": _gate(
            stats["max_drawdown_pct"],
            th["max_drawdown_pct"],
            stats["max_drawdown_pct"] < th["max_drawdown_pct"],
        ),
        "pessimistic_queue_profitable": _gate(
            agg["pessimistic"]["stats"]["net_usd"], 0.0, agg["pessimistic"]["stats"]["net_usd"] > 0
        ),
        "latency_2x_profitable": _gate(
            agg["latency_2x"]["stats"]["net_usd"], 0.0, agg["latency_2x"]["stats"]["net_usd"] > 0
        ),
        "signal_edge_covers_cost": signal_edge_gate(base, horizon_s, cost_bp),
    }


def write_gates(model_path: Path, verdict: dict[str, Any]) -> Path:
    """Add ``gates.json`` to a rule model and pin it in the manifest; never replaces one."""
    from ofi_scalper_service.model import _sha256

    target = model_path / "gates.json"
    if target.exists():
        raise FileExistsError(f"{target} exists: a model's verdict is written once")
    manifest_path = model_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    target.write_text(json.dumps({"model_version": manifest["version"], **verdict}, indent=2))
    manifest["files"]["gates.json"] = _sha256(target)
    manifest["data"] = {**(manifest.get("data") or {}), "gates": "research.evaluate_rules"}
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return target


def _report_md(version: str, days: list[str], verdict: dict[str, Any], arms: str) -> str:
    lines = [
        f"# {version}: {'PASSED' if verdict['passed'] else 'did not pass'}",
        "",
        f"Days: {', '.join(days)}. Queue fills at 1x latency; fees {verdict['fees_bp']} bp.",
        "",
        "| gate | value | threshold | passed |",
        "|---|---|---|---|",
    ]
    for name, g in verdict["gates"].items():
        mark = "yes" if g["passed"] else ("no" if g.get("required", True) else "n/a")
        lines.append(f"| {name} | {g['value']} | {g['threshold']} | {mark} |")
    lines += ["", "## Arms", "", arms, ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research.evaluate_rules", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument("--version", required=True, help="a rule model under OFI_MODEL_DIR")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--arm", action="append", default=[], metavar="NAME:FIELD=VALUE,...")
    parser.add_argument("--order-rtt-ms", type=float, default=None)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--write-gates", action="store_true")
    args = parser.parse_args(argv)

    from ofi_scalper_service.config import load_settings
    from ofi_scalper_service.model import load_model

    settings = load_settings(args.profile)
    model = load_model(settings.model_dir, args.version, require_pass=False)
    if model.kind != "rules":
        parser.error(f"{args.version} is a {model.kind} model: research.pipeline gates those")
    days, excluded = eligible_days(
        settings.record_dir,
        days_between(args.start, args.end),
        host_tag=settings.host_tag,
        symbols=settings.research_symbols,
    )
    for day, why in excluded.items():
        print(json.dumps({"excluded": day, "why": why}))
    if not days:
        parser.error("no eligible days in that range")

    path = str(settings.model_dir / args.version)
    specs = [
        VariantSpec("base", path, "rules", 0.5, "queue", 1.0),
        VariantSpec("pessimistic", path, "rules", 0.5, "pessimistic", 1.0),
        VariantSpec("latency_2x", path, "rules", 0.5, "queue", 2.0),
    ]
    for text in args.arm:
        name, extra = parse_arm(text)
        specs.append(VariantSpec(name, path, "rules", 0.5, "queue", 1.0, policy_extra=extra))
    latency = Latency.from_file(Path("research/latency.json"), order_rtt_ms=args.order_rtt_ms)
    common = job_settings(settings)
    jobs = [DayJob(day, str(settings.record_dir), tuple(specs), latency, **common) for day in days]
    agg = aggregate(run_days(jobs, args.jobs), capital_usd=common["shadow_equity_usd"])

    cost_bp = common["maker_bp"] + common["taker_bp"]
    gates = rules_gate_table(agg, days=len(days), horizon_s=model.policy.horizon_s, cost_bp=cost_bp)
    verdict = {
        "passed": passed_all(gates),
        "binding": False,
        "tier": "rules-shallow" if len(days) < FULL_THRESHOLD_DAYS else "rules",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "gates": gates,
        "days": days,
        "fees_bp": {"maker": common["maker_bp"], "taker": common["taker_bp"]},
        "latency": {"feed_ms": latency.feed_ms, "order_ms": latency.order_ms},
        "policy": model.policy.as_dict(),
    }
    arms = compare_arms(agg)
    out = settings.research_dir / "rules" / args.version / f"{days[0]}_{days[-1]}"
    out.mkdir(parents=True, exist_ok=True)
    summary = {name: {k: v for k, v in a.items() if k != "trades"} for name, a in agg.items()}
    (out / "evaluation.json").write_text(
        json.dumps({**verdict, "arms": summary}, indent=2, default=str)
    )
    (out / "REPORT.md").write_text(_report_md(args.version, days, verdict, arms))
    if args.write_gates:
        print(f"wrote {write_gates(settings.model_dir / args.version, verdict)}")
    print(_report_md(args.version, days, verdict, arms))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
