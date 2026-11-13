"""Check the vectorised signals against the live service code, minute by minute.

Replays a slice through ipda's own Aggregator + ReversalSignalStrategy + SignalGate
with the live 600-minute lookback and compares the emitted (bucket, direction) set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # makes `src` importable as a package
sys.path.insert(0, str(HERE))

from data import load_minutes  # noqa: E402
from engine import build_buckets, reversal_signals  # noqa: E402

from src.candles import Aggregator, Candle  # noqa: E402
from src.signal_gate import SignalGate  # noqa: E402
from src.strategy import ReversalParams, ReversalSignalStrategy  # noqa: E402


def main(start: str = "2025-03-03", end: str = "2025-03-08") -> int:
    df = load_minutes()
    lo = df["ts"].searchsorted(pd.Timestamp(start, tz="UTC"))
    hi = df["ts"].searchsorted(pd.Timestamp(end, tz="UTC"))
    warm = max(0, lo - 20_000)
    sub = df.iloc[warm:hi].reset_index(drop=True)

    b = build_buckets(sub)
    vec = reversal_signals(sub, b)
    vec = vec[vec["i"] >= lo - warm]
    got_vec = {
        (pd.Timestamp(sub["ts"][b.last_minute[g]]).floor("3min"), d)
        for g, d in zip(vec["group"], vec["dir"], strict=True)
    }

    agg = Aggregator(3)
    strat = ReversalSignalStrategy(ReversalParams())
    gate = SignalGate()
    candles = [
        Candle(r.ts.to_pydatetime(), r.open, r.high, r.low, r.close, 0.0) for r in sub.itertuples()
    ]
    got_live = set()
    for j in range(lo - warm, len(candles)):
        window = candles[max(0, j - 599) : j + 1]
        series = agg.build(window)
        bs = series.forming.start
        if gate.is_locked("X", bs):
            continue
        dec = strat.evaluate(series)
        if dec is None:
            continue
        gate.lock("X", bs)
        got_live.add((pd.Timestamp(bs), 1 if dec.direction == "buy" else -1))

    both = got_vec & got_live
    print(f"live={len(got_live)} vectorised={len(got_vec)} common={len(both)}")
    print("only live:", sorted(got_live - got_vec)[:10])
    print("only vectorised:", sorted(got_vec - got_live)[:10])
    return 0 if got_vec == got_live else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
