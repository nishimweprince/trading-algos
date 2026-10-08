"""PLAN 3.2: decide once, fill many, on our own tape (full depth).

    python -m research.fills run --record-dir data/raw/dev --start 20261006_19 \
        --out research/data/fills_dublin [--shift-ms k=0,p=0]
    python -m research.fills grid --run research/data/fills_dublin

A decision does not depend on latency or on the limit, so the engine runs once.
``run`` replays the tape at the given parameters (``research.replay``) and, for
every entry, taps the cheap leg's ask ladder at each event for the next
``HORIZON_MS``. The engine resolves an order on the first event at or past
``decision + latency``, against the book *before* that event is applied; the tap
records exactly that state, so a fill at any latency can be computed from it.
Written: ``fills.parquet`` (one row per entry per event), the replay's
``journal/`` and ``summary.json``.

``grid`` fills every combination of ``LATENCIES_MS`` x ``OFFSETS_C`` (limit = touch
+ offset ¢, never above the cap; same ordered size), walking the ladder as
``PaperVenue`` does, and settles on the journal's Polymarket resolutions. It first
checks the engine: at the replay's own latency and offset 0, every entry must
fill exactly as the replay's ``fill`` record did. Output ``fill_grid.json``:
fill rate, share of the order filled, VWAP, net ¢/share at the real fill and
$ filled per entry, per (latency, offset), overall and per asset.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poly_kalshi_service.decision import StrategyParams
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.fees import taker_fee
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.trades import read_journal
from research.replay import ALL_ASSETS, replay, shift_lines

NS = 1_000_000_000
HORIZON_MS = 1000
LATENCIES_MS = (0, 25, 50, 100, 150, 250, 400)
OFFSETS_C = (0, 1, 2, 3)
LADDER_ABOVE = 0.05  # levels kept above the limit: offsets reach +3¢


@dataclass
class _Capture:
    asset: str
    open_unix: int
    decision_ns: int
    token: str
    side: str
    limit: float
    contracts: int
    fee_rate: float
    fee_exponent: float
    rows: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


class LadderTap:
    """Wraps the line stream: snapshots open captures before each line, starts new ones after."""

    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.engine: Engine | None = None
        self.open: list[_Capture] = []
        self.rows: list[dict[str, Any]] = []
        self._seen: dict[str, Any] = {}

    def lines(self, lines: Iterator[tuple[int, str, str]]) -> Iterator[tuple[int, str, str]]:
        for recv_ns, venue, text in lines:
            if self.open:
                self._snapshot(recv_ns)
            yield recv_ns, venue, text
            self._start(recv_ns)

    def _snapshot(self, recv_ns: int) -> None:
        assert self.engine is not None
        still = []
        for c in self.open:
            book = self.engine.books.get(c.token)
            ladder = (
                []
                if book is None or book.snapshot_ns is None
                else book.asks_upto(c.limit + LADDER_ABOVE)
            )
            dt = recv_ns - c.decision_ns
            self.rows.append(
                {"asset": c.asset, "open_unix": c.open_unix, "decision_ns": c.decision_ns,
                 "dt_ns": dt, "px": [p for p, _ in ladder], "sz": [s for _, s in ladder]}
            )  # fmt: skip
            if dt <= HORIZON_MS * 1_000_000:
                still.append(c)
        self.open = still

    def _start(self, recv_ns: int) -> None:
        assert self.engine is not None
        for asset, st in self.engine.state.items():
            d = st.decision
            if d is None or self._seen.get(asset) is d:
                continue
            self._seen[asset] = d
            if d["action"] != "enter" or d.get("blocked") or d["t_ns"] != recv_ns:
                continue
            w = st.window
            assert w is not None
            buy_yes = d["side"] == "yes"
            c = _Capture(
                asset, w.open_unix, recv_ns, w.yes_token if buy_yes else w.no_token, d["side"],
                d["entry_px"], d["contracts"], w.fee_rate, w.fee_exponent,
            )  # fmt: skip
            self.open.append(c)
            self.rows.append(
                {"asset": asset, "open_unix": w.open_unix, "decision_ns": recv_ns, "dt_ns": None,
                 "side": d["side"], "limit": d["entry_px"], "contracts": d["contracts"],
                 "fee_rate": w.fee_rate, "fee_exponent": w.fee_exponent,
                 "sec_into": d["sec_into"], "gap": d["gap"]}
            )  # fmt: skip


def fill_at(
    events: list[dict[str, Any]],
    latency_ms: float,
    limit: float,
    contracts: int,
    rate: float,
    exp: float,
) -> tuple[float, float, float]:
    """``(shares, cost, fee)`` against the ladder before the first event at or past the latency."""
    due = round(latency_ms * 1_000_000)
    for e in events:
        if e["dt_ns"] >= due:
            remaining = float(contracts)
            cost = fee = 0.0
            for p, s in zip(e["px"], e["sz"], strict=True):
                if p > limit + 1e-9 or remaining <= 0:
                    break
                take = min(s, remaining)
                cost += take * p
                fee += taker_fee(p, take, rate, exp)
                remaining -= take
            return contracts - remaining, cost, fee
    return 0.0, 0.0, 0.0  # the tape ended before the order would have landed


def grid(run: Path, cap: float) -> dict[str, Any]:
    import pandas as pd

    df = pd.read_parquet(run / "fills.parquet")
    journal = list(read_journal(run / "journal"))
    outcomes = {
        (r["asset"], r["open_unix"]): r["outcome"]
        for r in journal
        if r.get("type") == "resolution" and r.get("venue") == "poly"
    }
    engine_fills = {(r["asset"], r["open_unix"]): r for r in journal if r.get("type") == "fill"}
    summary = json.loads((run / "summary.json").read_text())
    own_latency = summary["params"]["latency_ms"]

    entries = []
    for (asset, open_unix, _decision), g in df.groupby(["asset", "open_unix", "decision_ns"]):
        head = g[g["dt_ns"].isna()].iloc[0]
        events = g[g["dt_ns"].notna()].sort_values("dt_ns").to_dict("records")
        entries.append((asset, int(open_unix), head, events))

    # The engine check: own latency, offset 0 = the replay's fill record, exactly.
    mismatches = []
    for asset, open_unix, h, events in entries:
        shares, cost, fee = fill_at(
            events, own_latency, h["limit"], h["contracts"], h["fee_rate"], h["fee_exponent"]
        )
        f = engine_fills.get((asset, open_unix))
        same = f is not None and abs(f["shares"] - shares) <= 1e-6
        if not same or abs(f["cost"] - round(cost, 4)) > 1e-4:
            mismatches.append(
                {"asset": asset, "open_unix": open_unix, "grid": [shares, cost],
                 "engine": None if f is None else [f["shares"], f["cost"]]}
            )  # fmt: skip

    cells: dict[str, Any] = {}
    for latency in LATENCIES_MS:
        for offset in OFFSETS_C:
            rows = []
            for asset, open_unix, h, events in entries:
                limit = min(round(h["limit"] + offset / 100, 4), cap)
                shares, cost, fee = fill_at(
                    events, latency, limit, h["contracts"], h["fee_rate"], h["fee_exponent"]
                )
                outcome = outcomes.get((asset, open_unix))
                win = None if outcome is None else float(outcome == h["side"])
                rows.append(
                    {"asset": asset, "shares": shares, "ordered": h["contracts"], "cost": cost,
                     "fee": fee, "win": win}
                )  # fmt: skip
            cells[f"{latency}ms+{offset}c"] = {
                "all": _cell(rows),
                "by_asset": {
                    a: _cell([r for r in rows if r["asset"] == a])
                    for a in sorted({r["asset"] for r in rows})
                },
            }
    return {
        "entries": len(entries),
        "engine_check": {
            "latency_ms": own_latency,
            "mismatches": mismatches,
            "agreement": None if not entries else 1 - len(mismatches) / len(entries),
        },
        "cells": cells,
    }


def _cell(rows: list[dict[str, Any]]) -> dict[str, Any]:
    filled = [r for r in rows if r["shares"] > 0]
    settled = [r for r in filled if r["win"] is not None]
    shares = sum(r["shares"] for r in filled)
    nets = [100 * (r["win"] * r["shares"] - r["cost"] - r["fee"]) / r["shares"] for r in settled]
    return {
        "entries": len(rows),
        "fill_rate": None if not rows else len(filled) / len(rows),
        "fill_ratio": None
        if not filled
        else statistics.fmean(r["shares"] / r["ordered"] for r in filled),
        "usd_filled_per_entry": None if not rows else statistics.fmean(r["cost"] for r in rows),
        "vwap": None if not filled else sum(r["cost"] for r in filled) / shares,
        "settled": len(settled),
        "win_rate": None if not settled else statistics.fmean(r["win"] for r in settled),
        "net_cents": None if not nets else statistics.fmean(nets),
        "net_se": None if len(nets) < 2 else statistics.stdev(nets) / len(nets) ** 0.5,
        "pnl_usd": round(sum(r["win"] * r["shares"] - r["cost"] - r["fee"] for r in settled), 2),
    }  # fmt: skip


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.fills")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--record-dir", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--start")
    run.add_argument("--end")
    run.add_argument("--assets", default=ALL_ASSETS)
    run.add_argument("--gap", type=float, default=0.10)
    run.add_argument("--cap", type=float, default=0.60)
    run.add_argument("--early", type=int, default=20)
    run.add_argument("--late", type=int, default=60)
    run.add_argument("--latency-ms", type=int, default=150)
    run.add_argument("--shift-ms", default="")
    g = sub.add_parser("grid")
    g.add_argument("--run", type=Path, required=True)
    g.add_argument("--cap", type=float, default=0.60)
    args = parser.parse_args()

    if args.cmd == "grid":
        result = grid(args.run, args.cap)
        (args.run / "fill_grid.json").write_text(json.dumps(result, indent=1, default=str))
        print(json.dumps({k: v for k, v in result.items() if k != "cells"}, indent=1, default=str))
        return

    import pandas as pd

    files = iter_files(args.record_dir, args.start, args.end)
    if not files:
        raise SystemExit(f"no recordings under {args.record_dir}")
    stats: dict[str, int] = {}
    params = StrategyParams(args.gap, args.cap, 50.0, args.early, args.late)
    shift = {
        v.strip(): float(ms)
        for v, _, ms in (p.partition("=") for p in args.shift_ms.split(",") if p)
    }
    lines = read_lines(files, stats)
    if any(shift.values()):
        lines = shift_lines(lines, shift, stats)
    tap = LadderTap(args.cap)
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip())
    args.out.mkdir(parents=True, exist_ok=True)

    engine, _ = replay(
        tap.lines(lines), params, assets, latency_ms=args.latency_ms,
        journal_dir=args.out / "journal", sample=False, shadow="",
        on_engine=lambda e: setattr(tap, "engine", e),
    )  # fmt: skip
    pd.DataFrame(tap.rows).to_parquet(args.out / "fills.parquet", index=False)
    summary = {
        "files": [str(f) for f in files],
        "reader": stats,
        "counters": dict(engine.counters),
        "params": vars(args) | {"record_dir": str(args.record_dir), "out": str(args.out)},
        "shift_ms": shift,
        "feed_ms_k": engine.metrics.snapshot().get("feed_ms_k"),
        "feed_ms_p": engine.metrics.snapshot().get("feed_ms_p"),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: summary[k] for k in ("reader", "counters")}, indent=1))


if __name__ == "__main__":
    _main()
