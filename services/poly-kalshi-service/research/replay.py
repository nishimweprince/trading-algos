"""Replay the tape through the live engine, and compare with what the live process did.

    python -m research.replay run --record-dir data/raw/dev --out research/data/run1
    python -m research.replay compare --live data/state/dev/journal \
        --replay research/data/run1/journal

``run`` pushes every recorded line through ``Engine.handle_line`` with a fresh
in-memory ledger and the paper venue, exactly as live. It writes:

- ``journal/``: the same decision journal the live process writes
- ``grid.parquet``: one row per asset per second of every identified window
  with all three legs' quotes as they stood at the end of that second, for
  ``research.nulls``

``compare`` matches decisions by (asset, window) and reports any window where
replay and live disagree on the action, the leg or the entry price. On the
same tape they must agree exactly; any difference is a bug in determinism.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from poly_kalshi_service.decision import WINDOW_S, StrategyParams
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.ledger import Ledger
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.risk import RiskLimits, RiskState
from poly_kalshi_service.trades import JsonlDaily, read_journal
from poly_kalshi_service.venue import PaperVenue

__all__ = ["GridSampler", "compare", "replay"]

NS = 1_000_000_000
GRID_COLUMNS = (
    "asset", "open_unix", "sec", "k_bid", "k_ask", "y_bid", "y_ask", "n_bid", "n_ask",
)  # fmt: skip


class GridSampler:
    """Per-second snapshots of each asset's three quotes, taken before the next event."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.rows: list[tuple[Any, ...]] = []
        self._last_sec: int | None = None

    def before(self, recv_ns: int) -> None:
        sec = recv_ns // NS
        if self._last_sec is None:
            self._last_sec = sec
            return
        # The book is constant between events: the state now is the state at
        # the end of every whole second since the last event.
        for s in range(self._last_sec, min(sec, self._last_sec + WINDOW_S)):
            self._sample(s)
        self._last_sec = sec

    def _sample(self, sec: int) -> None:
        for asset, st in self.engine.state.items():
            window = st.window
            if window is None or not window.open_unix <= sec < window.close_unix:
                continue
            quotes = self.engine.quotes(asset)
            if quotes is None or not all(q.valid for q in quotes):
                continue
            k, y, n = quotes
            self.rows.append(
                (asset, window.open_unix, sec - window.open_unix, k.bid, k.ask, y.bid, y.ask,
                 n.bid, n.ask)
            )  # fmt: skip


def replay(
    lines: Iterator[tuple[int, str, str]],
    params: StrategyParams,
    assets: tuple[str, ...],
    *,
    latency_ms: int = 150,
    start_cash: float = 1e9,
    journal_dir: Path | None = None,
    sample: bool = True,
) -> tuple[Engine, GridSampler | None]:
    engine = Engine(
        params,
        assets,
        ledger=Ledger(start_cash=start_cash, cash=start_cash),
        risk=RiskState(RiskLimits(max_open_notional_usd=1e12, daily_loss_limit_usd=1e12)),
        venue=PaperVenue(latency_ms),
        journal=JsonlDaily(journal_dir),
    )
    sampler = GridSampler(engine) if sample else None
    for recv_ns, venue, text in lines:
        if sampler is not None:
            sampler.before(recv_ns)
        engine.handle_line(recv_ns, venue, text)
    return engine, sampler


def compare(live: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> dict[str, Any]:
    def by_window(records: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
        return {(r["asset"], r["open_unix"]): r for r in records if r.get("type") == "decision"}

    a, b = by_window(live), by_window(replayed)
    common = sorted(set(a) & set(b))
    mismatches = []
    for key in common:
        fields = ("action", "side", "entry_px", "contracts")
        if any(a[key].get(f) != b[key].get(f) for f in fields):
            mismatches.append(
                {"window": key, "live": {f: a[key].get(f) for f in fields},
                 "replay": {f: b[key].get(f) for f in fields}}
            )  # fmt: skip
    return {
        "live_only": sorted(set(a) - set(b)),
        "replay_only": sorted(set(b) - set(a)),
        "common": len(common),
        "mismatches": mismatches,
        "agreement": None if not common else 1 - len(mismatches) / len(common),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.replay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--record-dir", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--start", help="YYYYMMDD[_HH]")
    run.add_argument("--end", help="YYYYMMDD[_HH]")
    run.add_argument("--assets", default="BTC,ETH,SOL,XRP,BNB")
    run.add_argument("--gap", type=float, default=0.20)
    run.add_argument("--cap", type=float, default=0.60)
    run.add_argument("--notional", type=float, default=50.0)
    run.add_argument("--early", type=int, default=20)
    run.add_argument("--late", type=int, default=60)
    run.add_argument("--latency-ms", type=int, default=150)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--live", type=Path, required=True)
    cmp_.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args()

    if args.cmd == "compare":
        result = compare(list(read_journal(args.live)), list(read_journal(args.replay)))
        print(json.dumps(result, indent=2, default=str))
        return

    import pandas as pd

    files = iter_files(args.record_dir, args.start, args.end)
    if not files:
        raise SystemExit(f"no recordings under {args.record_dir}")
    stats: dict[str, int] = {}
    params = StrategyParams(args.gap, args.cap, args.notional, args.early, args.late)
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip())
    args.out.mkdir(parents=True, exist_ok=True)
    engine, sampler = replay(
        read_lines(files, stats),
        params,
        assets,
        latency_ms=args.latency_ms,
        journal_dir=args.out / "journal",
    )
    assert sampler is not None
    pd.DataFrame(sampler.rows, columns=list(GRID_COLUMNS)).to_parquet(args.out / "grid.parquet")
    summary = {
        "files": len(files),
        "reader": stats,
        "counters": dict(engine.counters),
        "grid_rows": len(sampler.rows),
        "params": vars(args) | {"record_dir": str(args.record_dir), "out": str(args.out)},
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    _main()
