"""Replay the packed M1 history through the live engine with the research fill simulator.

    wfp-replay --data ~/Documents/Markets/Docs/Walk\\ Forward/reference/data --out out/replay [--symbols XAUUSD ...]

Writes `<out>/Sxx_trades.csv` (same columns as reference/trades) and prints the parity table when `--reference`
points at the reference trades folder.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

from . import config as cfgmod
from .clock import fmt
from .engine import Market
from .exits import SimPosition

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "strategies.yaml"


def load_npz(path: Path):
    z = np.load(path)
    return z["t"].astype(np.int64), z["o"], z["h"], z["l"], z["c"]


def replay_symbol(sym: str, data_dir: Path, cfg: cfgmod.Config, start: int | None = None, end: int | None = None,
                  progress: bool = True):
    t, o, h, l, c = load_npz(data_dir / f"{sym}_m1.npz")
    strategies = cfg.by_symbol(sym, enabled_only=False)
    mk = Market(sym, strategies, cfg.cost[sym])
    pip = cfg.pip[sym]
    by_id = {s.id: s for s in strategies}
    open_pos: dict[str, SimPosition] = {}
    trades: dict[str, list[SimPosition]] = {s.id: [] for s in strategies}
    skips: Counter = Counter()

    def is_flat(sid: str) -> bool:
        return sid not in open_pos

    tl, ol, hl, ll, cl = t.tolist(), o.tolist(), h.tolist(), l.tolist(), c.tolist()
    n = len(tl)
    prev_t = None
    t0 = time.time()
    for k in range(n):
        tk = tl[k]
        if start is not None and tk < start:
            continue
        if end is not None and tk >= end:
            break
        ok, hk, lk, ck = ol[k], hl[k], ll[k], cl[k]
        entries, sk = mk.on_m1(tk, ok, hk, lk, ck, is_flat)
        for s in sk:
            skips[(s.strategy.id, s.reason)] += 1
        for e in entries:
            st = by_id[e.strategy.id]
            X = st.stop_pips * pip
            open_pos[st.id] = SimPosition(st.id, e.side, k, tk, ok, ok - e.side * X, ok + e.side * 2 * X, X,
                                          cfg.cost[sym], e.signal.bar_start)
        for sid in list(open_pos):
            p = open_pos[sid]
            if p.on_m1(k, tk, prev_t if k > p.j else None, ok, hk, lk, ck):
                trades[sid].append(p)
                del open_pos[sid]
        prev_t = tk
        if progress and k % 500000 == 0 and k:
            print(f"  {sym} {fmt(tk)} {k}/{n} {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    return trades, skips


def write_trades(path: Path, rows: list[SimPosition]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal_bar_est", "entry_time_est", "side", "entry", "stop", "take_profit", "exit_time_est",
                    "exit_price", "exit_reason", "hold_minutes", "R_net"])
        for p in rows:
            w.writerow([fmt(p.signal_bar), fmt(p.t_entry), "BUY" if p.side == 1 else "SELL", f"{p.e:.5f}",
                        f"{p.sl:.5f}", f"{p.tp:.5f}", fmt(p.exit_t), f"{p.exit_px:.5f}", p.reason, p.exit_k - p.j,
                        f"{p.R:.5f}"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wfp-replay")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--reference", type=Path, help="reference/trades folder: print the parity table")
    a = ap.parse_args(argv)
    cfg = cfgmod.load(a.config)
    a.out.mkdir(parents=True, exist_ok=True)
    for sym in a.symbols or cfg.symbols:
        trades, skips = replay_symbol(sym, a.data, cfg)
        for sid, rows in trades.items():
            write_trades(a.out / f"{sid}_trades.csv", rows)
        print(sym, "skips", dict(sorted(skips.items())), file=sys.stderr)
    if a.reference:
        from .parity import report

        ok = report(a.out, a.reference, cfg)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
