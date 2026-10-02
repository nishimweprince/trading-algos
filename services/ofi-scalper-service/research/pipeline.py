"""Stage 2 end to end: eligible days -> features -> train -> select -> gates.

    python -m research.pipeline --profile dev --provisional [--to 2026-11-01] [--jobs 3]
    python -m research.pipeline --profile dev --binding --run binding-1

Each step reuses what it already wrote for this run (the split is fixed per
run name), so a re-run after a crash continues where it stopped. The held-out
period is evaluated once per run; a second ``gates`` on it is refused.
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ofi_scalper_service.daily_check import summarise as day_summary

from .backtest import Latency, days_between, job_settings
from .cv import load_or_create_split
from .dataset import HORIZONS_S
from .gates import evaluate
from .replay import _write_rows, find_session, hour_files, replay_files
from .selection import select
from .train import Candidate, train_candidate

__all__ = ["eligible_days", "ensure_features", "main"]


def eligible_days(
    record_dir: Path, days: list[str], *, host_tag: str, book_mode: str = "diff"
) -> tuple[list[str], dict[str, str]]:
    """Days the gates may use: complete, clean, recorded on the right host and mode."""
    keep: list[str] = []
    excluded: dict[str, str] = {}
    for day in days:
        d = date.fromisoformat(day)
        summary = day_summary(record_dir, d)
        if not summary["ok"]:
            excluded[day] = "; ".join(summary["problems"])
            continue
        stamp = d.strftime("%Y%m%d")
        hosts = {
            json.loads(p.read_text()).get("host")
            for p in record_dir.glob(f"*/{stamp}/*.gz.json")
            if not p.parent.parent.name.startswith("_")
        }
        if hosts != {host_tag}:
            excluded[day] = f"recorded on {sorted(h or '?' for h in hosts)}, need {host_tag}"
            continue
        session = find_session(record_dir, datetime(d.year, d.month, d.day, 23, tzinfo=UTC))
        mode = (session or {}).get("book_mode")
        if mode != book_mode:
            excluded[day] = f"book mode {mode}, need {book_mode}"
            continue
        keep.append(day)
    return keep, excluded


def _features_day(args: tuple[str, str, str, dict[str, Any]]) -> str:
    record_dir, out_dir, day, overrides = args
    logging.disable(logging.WARNING)
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    end_ns = int((start + timedelta(days=1)).timestamp() * 1e9)
    rows, _ = replay_files(
        hour_files(Path(record_dir), start, 24),
        session=find_session(Path(record_dir), start),
        keep=lambda r: r["t_ns"] < end_ns,
        **overrides,
    )
    return str(_write_rows(rows, Path(out_dir) / f"{day}.parquet"))


def ensure_features(
    record_dir: Path, features_dir: Path, days: list[str], overrides: dict[str, Any], n_jobs: int
) -> list[str]:
    """Replay any day that has no feature file yet."""
    missing = [d for d in days if not (features_dir / f"{d}.parquet").is_file()]
    work = [(str(record_dir), str(features_dir), d, overrides) for d in missing]
    if n_jobs <= 1 or len(work) <= 1:
        return [_features_day(w) for w in work]
    with ProcessPoolExecutor(max_workers=n_jobs) as pool:
        return list(pool.map(_features_day, work))


def _say(step: str, **fields: Any) -> None:
    print(json.dumps({"step": step, **fields}, default=str), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2: train, select and gate a model")
    parser.add_argument("--profile", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provisional", action="store_true", help="weekly periods, non-binding")
    mode.add_argument("--binding", action="store_true", help="monthly periods, binding")
    parser.add_argument("--run", default=None, help="run name; its split is fixed once made")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    parser.add_argument("--horizons", default=",".join(f"{h:g}" for h in HORIZONS_S))
    parser.add_argument("--barrier-bp", type=float, default=6.0)
    parser.add_argument("--order-rtt-ms", type=float, default=None)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--host-tag", default=None)
    args = parser.parse_args(argv)

    from ofi_scalper_service.config import load_settings

    settings = load_settings(args.profile)
    research_dir = settings.research_dir
    record_dir = settings.record_dir
    features_dir = research_dir / "features" / (settings.profile or "default")
    run = args.run or ("provisional-1" if args.provisional else "binding-1")
    period = "week" if args.provisional else "month"

    end = args.end or (datetime.now(UTC).date() - timedelta(days=1))
    start = args.start or _first_recorded_day(record_dir) or end
    days, excluded = eligible_days(
        record_dir, days_between(start, end), host_tag=args.host_tag or settings.host_tag
    )
    _say("eligible_days", eligible=len(days), excluded=excluded)
    split = load_or_create_split(
        research_dir / "splits", run, days=days, period=period, binding=args.binding
    )
    _say("split", walk_forward=split.walk_forward, held_out=split.holdout)

    overrides = {
        "grid_ms": settings.grid_ms,
        "burst_trades": settings.burst_trades,
        "stale_book_ms": settings.stale_book_ms,
    }
    all_days = split.days_of(split.walk_forward) + split.days_of(split.holdout)
    _say(
        "features",
        written=ensure_features(record_dir, features_dir, all_days, overrides, args.jobs),
    )

    candidates = []
    for horizon in (float(h) for h in args.horizons.split(",")):
        out = research_dir / "candidates" / run / f"h{horizon:g}"
        if (out / "candidate.json").is_file():
            candidates.append(Candidate.load(out))
        else:
            candidates.append(
                train_candidate(
                    split,
                    features_dir=features_dir,
                    out_dir=out,
                    horizon_s=horizon,
                    barrier_bp=args.barrier_bp,
                )
            )
        _say("candidate", horizon_s=horizon, path=str(out))

    latency = Latency.from_file(Path("research/latency.json"), order_rtt_ms=args.order_rtt_ms)
    common = job_settings(settings)
    selection_path = research_dir / "candidates" / run / "selection.json"
    if selection_path.is_file():
        selection = json.loads(selection_path.read_text())
    else:
        selection = select(
            candidates,
            split,
            record_dir=record_dir,
            common=common,
            latency=latency,
            out=selection_path.parent,
            n_jobs=args.jobs,
        )
    _say("selection", chosen=selection["chosen"])

    result = evaluate(
        split,
        selection,
        record_dir=record_dir,
        features_dir=features_dir,
        research_dir=research_dir,
        models_dir=settings.model_dir,
        common=common,
        latency=latency,
        daily_loss_limit_usd=settings.daily_loss_limit_usd,
        n_jobs=args.jobs,
    )
    _say("gates", **result)
    return 0 if result.get("passed") else 1


def _first_recorded_day(record_dir: Path) -> date | None:
    days = sorted(p.name for p in record_dir.glob("*/*") if p.is_dir() and p.name.isdigit())
    return datetime.strptime(days[0], "%Y%m%d").date() if days else None


if __name__ == "__main__":
    raise SystemExit(main())
