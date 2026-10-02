"""The backtest: recordings replayed through the live code, with simulated fills.

Each recorded line goes through the plugin's consumer and ``ScalperRuntime``,
exactly as live; each grid sample goes to an ``ExecutionBridge`` in shadow mode
whose venue simulates fills (``ShadowVenue``: pessimistic or queue-position
model, with latency). Scores come from precomputed tables (``scores.py``), so
the policy, risk, sizing, fees and records are the live ones.

Days are independent jobs (each starts from the top-of-hour book checkpoint)
and run in parallel processes. One pass over a day drives every variant.

    python -m research.backtest --profile dev --candidate research/data/candidates/p1/h5 \\
        --from 2026-10-05 --to 2026-10-11 --scores oof --threshold 0.55 --queue queue
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ofi_scalper_service.policy import PolicyParams
from ofi_scalper_service.risk import RiskLimits
from ofi_scalper_service.trades import summarise

from .replay import ShadowConfig, Variant, find_session, hour_files, replay_files
from .scores import PrecomputedScorer, load_table
from .train import Candidate

__all__ = [
    "BacktestModel",
    "DayJob",
    "Latency",
    "VariantSpec",
    "aggregate",
    "run_day",
    "run_days",
]

log = logging.getLogger(__name__)


@dataclass
class BacktestModel:
    """What the bridge reads from a model: version, policy, scorer."""

    version: str
    policy: PolicyParams
    scorer: Any
    engine: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Latency:
    """Decision-to-venue delay. Recordings already carry 1x feed latency."""

    feed_ms: float = 0.0  # one-way feed latency (p50), added again per extra multiple
    order_ms: float = 0.0  # one-way order latency (half the order round trip, p50)

    def delay_ns(self, multiple: float) -> int:
        return int(((multiple - 1) * self.feed_ms + multiple * self.order_ms) * 1e6)

    @classmethod
    def from_file(cls, path: Path, *, order_rtt_ms: float | None = None) -> Latency:
        """``research/latency.json`` from ``ofi-latency``; order RTT measured on testnet."""
        raw: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {}
        feed = ((raw.get("feed_latency_ms") or {}).get("depth") or {}).get("p50") or 0.0
        if order_rtt_ms is None:
            rtt = (
                raw.get("order_rtt_ms")
                or ((raw.get("rest_rtt_ms") or {}).get("signed"))
                or ((raw.get("rest_rtt_ms") or {}).get("unsigned"))
            )
            order_rtt_ms = float(rtt["p50"]) if isinstance(rtt, dict) and "p50" in rtt else 0.0
        return cls(feed_ms=max(float(feed), 0.0), order_ms=order_rtt_ms / 2)


@dataclass(frozen=True)
class VariantSpec:
    name: str
    candidate: str  # candidate directory
    scores: str  # "oof" | "final"
    threshold: float
    queue_model: str = "queue"
    latency_multiple: float = 1.0
    buffer_bp: float = 0.0
    entry_timeout_ms: int = 1000
    time_stop_mult: float = 2.0


@dataclass(frozen=True)
class DayJob:
    day: str
    record_dir: str
    specs: tuple[VariantSpec, ...]
    latency: Latency
    limits: RiskLimits
    gate: dict[str, Any]
    order_notional_usd: float
    maker_bp: float
    taker_bp: float
    shadow_equity_usd: float
    overrides: dict[str, Any] = field(default_factory=dict)


def _variants(job: DayJob) -> list[Variant]:
    tables: dict[tuple[str, str], PrecomputedScorer] = {}
    variants = []
    for spec in job.specs:
        candidate = Candidate.load(Path(spec.candidate))
        key = (spec.candidate, spec.scores)
        if key not in tables:
            folder = candidate.oof_dir() if spec.scores == "oof" else candidate.final_dir()
            tables[key] = PrecomputedScorer(load_table([folder / f"{job.day}.parquet"]))
        policy = PolicyParams(
            horizon_s=candidate.horizon_s,
            threshold=spec.threshold,
            barrier_bp=candidate.barrier_bp,
            buffer_bp=spec.buffer_bp,
            entry_timeout_ms=spec.entry_timeout_ms,
            time_stop_mult=spec.time_stop_mult,
        )
        variants.append(
            Variant(
                spec.name,
                BacktestModel(candidate.version, policy, tables[key]),
                queue_model=spec.queue_model,
                delay_ns=job.latency.delay_ns(spec.latency_multiple),
            )
        )
    return variants


def run_day(job: DayJob) -> dict[str, Any]:
    """Replay one UTC day for every variant; returns their trades, counts and halts."""
    logging.disable(logging.WARNING)
    record_dir = Path(job.record_dir)
    start = datetime.fromisoformat(job.day).replace(tzinfo=UTC)
    paths = hour_files(record_dir, start, 24)
    variants = _variants(job)
    shadow = ShadowConfig(
        limits=job.limits,
        gate=job.gate,
        order_notional_usd=job.order_notional_usd,
        maker_bp=job.maker_bp,
        taker_bp=job.taker_bp,
        shadow_equity_usd=job.shadow_equity_usd,
        variants=variants,
    )
    replay_files(
        paths,
        session=find_session(record_dir, start),  # in force when the day began
        keep=lambda _row: False,
        shadow=shadow,
        **job.overrides,
    )
    return {
        "day": job.day,
        "files": len(paths),
        "variants": {
            v.name: {
                "trades": list(v.book.recent_trades),
                "signals": len(v.book.recent_signals),
                "halts": v.halts,
                "scored": {"hits": v.model.scorer.hits, "misses": v.model.scorer.misses},
            }
            for v in variants
        },
    }


def run_days(jobs: list[DayJob], n_jobs: int = 1) -> list[dict[str, Any]]:
    if n_jobs <= 1 or len(jobs) <= 1:
        return [run_day(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=n_jobs) as pool:
        return list(pool.map(run_day, jobs))


# --- aggregation ---------------------------------------------------------------------


def aggregate(results: list[dict[str, Any]], *, capital_usd: float) -> dict[str, dict[str, Any]]:
    """Per variant: all trades, daily net P&L, the summary and the risk statistics."""
    out: dict[str, dict[str, Any]] = {}
    names = sorted({name for r in results for name in r["variants"]})
    for name in names:
        trades: list[dict[str, Any]] = []
        daily: dict[str, float] = {}
        halts: list[dict[str, Any]] = []
        for result in sorted(results, key=lambda r: r["day"]):
            if name not in result["variants"]:
                continue  # this variant did not run that day (e.g. stress days only)
            part = result["variants"][name]
            trades += part["trades"]
            halts += part["halts"]
            daily[result["day"]] = round(
                sum(t.get("net_usd", 0.0) for t in part["trades"] if t.get("filled")), 6
            )
        out[name] = {
            "summary": summarise(trades),
            "daily_net_usd": daily,
            "by_symbol_net_usd": _by(trades, "symbol"),
            "stats": risk_stats(list(daily.values()), capital_usd),
            "halts": halts,
            "trades": trades,
        }
    return out


def _by(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.get("filled") and t.get("net_usd") is not None:
            sums[str(t[key])] += t["net_usd"]
    return {k: round(v, 6) for k, v in sorted(sums.items())}


def risk_stats(daily_net: list[float], capital_usd: float) -> dict[str, Any]:
    """Annualised Sharpe of daily P&L (365 days: crypto trades every day), max drawdown."""
    n = len(daily_net)
    mean = sum(daily_net) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in daily_net) / (n - 1)) if n > 1 else 0.0
    equity = peak = capital_usd
    worst = 0.0
    for pnl in daily_net:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak * 100 if peak > 0 else 0.0)
    return {
        "days": n,
        "net_usd": round(sum(daily_net), 6),
        "sharpe": round(mean / sd * math.sqrt(365), 4) if sd > 0 else None,
        "max_drawdown_pct": round(worst, 4),
        "worst_day_usd": round(min(daily_net), 6) if daily_net else None,
        "positive_days": sum(1 for x in daily_net if x > 0),
    }


# --- CLI -----------------------------------------------------------------------------


def job_settings(settings: Any) -> dict[str, Any]:
    """The live profile's risk, gate, sizing and fees, for backtest jobs."""
    maker = settings.maker_fee_bp if settings.maker_fee_bp is not None else 2.0
    taker = settings.taker_fee_bp if settings.taker_fee_bp is not None else 5.0
    if settings.bnb_fee_discount:
        maker, taker = maker * 0.9, taker * 0.9
    return {
        "limits": RiskLimits.from_settings(settings),
        "gate": {
            "funding_blackout_minutes": settings.funding_blackout_minutes,
            "spread_pctl": settings.gate_spread_pctl,
            "vol_1m_bp": settings.gate_vol_1m_bp,
            "liq_burst_usd": settings.gate_liq_burst_usd,
        },
        "order_notional_usd": settings.order_notional_usd,
        "maker_bp": maker,
        "taker_bp": taker,
        "shadow_equity_usd": settings.shadow_equity_usd,
        "overrides": {
            "grid_ms": settings.grid_ms,
            "burst_trades": settings.burst_trades,
            "stale_book_ms": settings.stale_book_ms,
        },
    }


def days_between(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest a candidate on recordings")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--scores", choices=("oof", "final"), default="oof")
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--queue", choices=("pessimistic", "queue"), default="queue")
    parser.add_argument("--latency-mult", type=float, default=1.0)
    parser.add_argument("--order-rtt-ms", type=float, default=None)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    from ofi_scalper_service.config import load_settings

    settings = load_settings(args.profile)
    latency = Latency.from_file(Path("research/latency.json"), order_rtt_ms=args.order_rtt_ms)
    spec = VariantSpec(
        "run",
        str(args.candidate),
        args.scores,
        args.threshold,
        queue_model=args.queue,
        latency_multiple=args.latency_mult,
    )
    common = job_settings(settings)
    jobs = [
        DayJob(day, str(settings.record_dir), (spec,), latency, **common)
        for day in days_between(args.start, args.end)
    ]
    report = aggregate(run_days(jobs, args.jobs), capital_usd=settings.shadow_equity_usd)["run"]
    out = args.out or settings.research_dir / "backtests" / f"{args.candidate.name}-{args.start}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trades.jsonl").write_text(
        "".join(json.dumps(t, default=str) + "\n" for t in report.pop("trades"))
    )
    (out / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
