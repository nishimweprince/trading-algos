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

``--shift-ms k=…,p=…,c=0`` re-times a tape (PLAN 3.1): each venue's lines are
delayed by its shift and the streams re-merged by the new time, so a London
tape can stand in for Dublin's geometry. ``summary.json`` counts the lines whose
order changed and reports the tape's measured ``feed_ms_k`` / ``feed_ms_p``
(receive minus venue time), so each tape's geometry is measured, not assumed.
The shadow variants (``--shadow``, default the live service's) are decided too.
"""

from __future__ import annotations

import argparse
import heapq
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from poly_kalshi_service.decision import WINDOW_S, StrategyParams
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.ledger import Ledger
from poly_kalshi_service.metrics import Metrics
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.risk import RiskLimits, RiskState
from poly_kalshi_service.shadow import DEFAULT_VARIANTS, Shadow, parse_variants
from poly_kalshi_service.trades import JsonlDaily, read_journal
from poly_kalshi_service.venue import PaperVenue

__all__ = ["GridSampler", "compare", "replay", "shift_lines"]
ALL_ASSETS = "BTC,ETH,SOL,XRP,BNB,DOGE,HYPE"

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


def shift_lines(
    lines: Iterator[tuple[int, str, str]], shift_ms: dict[str, float], stats: dict[str, int]
) -> Iterator[tuple[int, str, str]]:
    """Delay each venue's lines by ``shift_ms[venue]`` and re-merge them by the new time.

    Within a venue the order is kept, so this is a merge of shifted streams. It
    runs in one pass: a line is released once no later input can come before it
    (input arrives in time order, so nothing can, past ``recv + min(shift)``).
    ``stats["reordered"]`` counts lines released after a line that came later
    in the file.
    """
    shifts = {v: round(ms * 1_000_000) for v, ms in shift_ms.items()}
    floor = min([0, *shifts.values()])
    heap: list[tuple[int, int, str, str]] = []
    stats.setdefault("reordered", 0)
    released = -1
    for seq, (recv_ns, venue, text) in enumerate(lines):
        heapq.heappush(heap, (recv_ns + shifts.get(venue, 0), seq, venue, text))
        horizon = recv_ns + floor
        while heap and heap[0][0] <= horizon:
            t, s, v, x = heapq.heappop(heap)
            if s < released:
                stats["reordered"] += 1
            released = max(released, s)
            yield t, v, x
    while heap:
        t, s, v, x = heapq.heappop(heap)
        if s < released:
            stats["reordered"] += 1
        released = max(released, s)
        yield t, v, x


def replay(
    lines: Iterator[tuple[int, str, str]],
    params: StrategyParams,
    assets: tuple[str, ...],
    *,
    latency_ms: int = 150,
    start_cash: float = 1e9,
    journal_dir: Path | None = None,
    sample: bool = True,
    shadow: str = "",
    on_engine: Callable[[Engine], None] | None = None,
) -> tuple[Engine, GridSampler | None]:
    engine = Engine(
        params,
        assets,
        ledger=Ledger(start_cash=start_cash, cash=start_cash),
        risk=RiskState(RiskLimits(max_open_notional_usd=1e12, daily_loss_limit_usd=1e12)),
        venue=PaperVenue(latency_ms),
        journal=JsonlDaily(journal_dir),
        shadow=Shadow(parse_variants(shadow, params), params),
    )
    # Feed geometry is read over the tape, not the live page's last 4,096 samples.
    engine.metrics = Metrics(window=2_000_000)
    if on_engine is not None:
        on_engine(engine)  # before the first line: a tap on the stream may read the engine
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
    run.add_argument("--assets", default=ALL_ASSETS)
    run.add_argument("--gap", type=float, default=0.10)
    run.add_argument("--cap", type=float, default=0.60)
    run.add_argument("--notional", type=float, default=50.0)
    run.add_argument("--early", type=int, default=20)
    run.add_argument("--late", type=int, default=60)
    run.add_argument("--latency-ms", type=int, default=150)
    run.add_argument("--shift-ms", default="", help="per-venue delay, e.g. k=9,p=-4 (PLAN 3.1)")
    run.add_argument(
        "--shadow", default=DEFAULT_VARIANTS, help="shadow variants JSON ('' for none)"
    )
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
    shift = {
        venue.strip(): float(ms)
        for venue, _, ms in (p.partition("=") for p in args.shift_ms.split(",") if p)
    }
    lines = read_lines(files, stats)
    if any(shift.values()):
        lines = shift_lines(lines, shift, stats)
    engine, sampler = replay(
        lines,
        params,
        assets,
        latency_ms=args.latency_ms,
        journal_dir=args.out / "journal",
        shadow=args.shadow,
    )
    assert sampler is not None
    pd.DataFrame(sampler.rows, columns=list(GRID_COLUMNS)).to_parquet(args.out / "grid.parquet")
    summary = {
        "files": len(files),
        "reader": stats,
        "counters": dict(engine.counters),
        "grid_rows": len(sampler.rows),
        "shift_ms": shift,
        # Receive minus venue time on this tape (after any shift): its measured geometry.
        "feed_ms_k": (engine.metrics.snapshot().get("feed_ms_k") or {}),
        "feed_ms_p": (engine.metrics.snapshot().get("feed_ms_p") or {}),
        "params": vars(args) | {"record_dir": str(args.record_dir), "out": str(args.out)},
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    _main()
