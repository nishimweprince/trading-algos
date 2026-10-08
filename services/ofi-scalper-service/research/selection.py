"""Choose the horizon and threshold by after-cost P&L, on validation periods only.

Every candidate (one per horizon) x threshold is backtested over the
walk-forward validation days with **out-of-fold** scores (the fold model never
saw the day), the **pessimistic** queue model and 1x latency. The pick is the
highest mean net P&L per day among arms with at least ``MIN_TRADES`` trades;
ties go to the higher threshold. Never F1, never the held-out period.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .backtest import DayJob, Latency, VariantSpec, aggregate, run_days
from .cv import Split, period_of
from .scores import load_table
from .train import Candidate

__all__ = ["MIN_TRADES", "THRESHOLDS", "score_diagnostics", "select"]

THRESHOLDS = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)
MIN_TRADES = 30


def _name(candidate: Candidate, threshold: float) -> str:
    return f"h{candidate.horizon_s:g}-t{threshold:.2f}"


def score_diagnostics(
    candidates: list[Candidate],
    days: list[str],
    thresholds: tuple[float, ...],
    *,
    maker_bp: float,
    buffer_bp: float = 0.0,
) -> list[dict[str, Any]]:
    """Why an arm can or cannot trade, from its out-of-fold scores alone.

    The policy enters only when p >= threshold and (p - p_opposite) x barrier
    >= 2 x maker fee + buffer, so with a small barrier no realistic p passes.
    Per arm: the scores clearing each condition and both, the probability gap
    the cost needs, and how high p gets (p99, max).
    """
    cost_bp = 2 * maker_bp + buffer_bp
    rows: list[dict[str, Any]] = []
    for c in candidates:
        table = load_table([c.oof_dir() / f"{day}.parquet" for day in days])
        if table.empty:
            continue
        up, down = table["up"], table["down"]
        p = up.where(up >= down, down)
        gap = (p - down.where(up >= down, up)).abs()
        edge_ok = gap * c.barrier_bp >= cost_bp
        for t in thresholds:
            above = p >= t
            rows.append(
                {
                    "name": _name(c, t),
                    "horizon_s": c.horizon_s,
                    "barrier_bp": c.barrier_bp,
                    "threshold": t,
                    "scores": int(len(table)),
                    "above_threshold": int(above.sum()),
                    "edge_ok": int(edge_ok.sum()),
                    "entries_possible": int((above & edge_ok).sum()),
                    "gap_needed": round(cost_bp / c.barrier_bp, 4),
                    "gap_max": round(float(gap.max()), 4),
                    "p_p99": round(float(p.quantile(0.99)), 4),
                    "p_max": round(float(p.max()), 4),
                }
            )
    return rows


def select(
    candidates: list[Candidate],
    split: Split,
    *,
    record_dir: Path,
    common: dict[str, Any],
    latency: Latency,
    out: Path,
    thresholds: tuple[float, ...] = THRESHOLDS,
    n_jobs: int = 1,
    min_trades: int = MIN_TRADES,
) -> dict[str, Any]:
    validation = list(split.walk_forward[1:])
    days = split.days_of(validation)
    specs = tuple(
        VariantSpec(_name(c, t), str(c.path), "oof", t, queue_model="pessimistic")
        for c in candidates
        for t in thresholds
    )
    jobs = [DayJob(day, str(record_dir), specs, latency, **common) for day in days]
    results = aggregate(run_days(jobs, n_jobs), capital_usd=common["shadow_equity_usd"])
    grid = []
    for c in candidates:
        for t in thresholds:
            arm = results[_name(c, t)]
            by_period: dict[str, float] = {}
            for day, net in arm["daily_net_usd"].items():
                key = period_of(day, split.period)
                by_period[key] = round(by_period.get(key, 0.0) + net, 6)
            grid.append(
                {
                    "name": _name(c, t),
                    "candidate": str(c.path),
                    "horizon_s": c.horizon_s,
                    "threshold": t,
                    "trades": arm["summary"]["trades"],
                    "net_usd": arm["stats"]["net_usd"],
                    "mean_daily_net_usd": round(arm["stats"]["net_usd"] / max(len(days), 1), 6),
                    "mean_net_bp": arm["summary"]["mean_net_bp"],
                    "win_rate": arm["summary"]["win_rate"],
                    "by_period_net_usd": by_period,
                }
            )
    eligible = [row for row in grid if row["trades"] >= min_trades]
    chosen = (
        max(eligible, key=lambda r: (r["mean_daily_net_usd"], r["threshold"])) if eligible else None
    )
    selection = {
        "rule": (
            f"max mean daily net P&L, >= {min_trades} trades; ties to the higher threshold; "
            "OOF scores, pessimistic queue, 1x latency, validation periods only"
        ),
        "validation_periods": validation,
        "days": days,
        "chosen": chosen,
        "grid": sorted(grid, key=lambda r: -r["mean_daily_net_usd"]),
        "diagnostics": score_diagnostics(candidates, days, thresholds, maker_bp=common["maker_bp"]),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "selection.json").write_text(json.dumps(selection, indent=2))
    return selection
