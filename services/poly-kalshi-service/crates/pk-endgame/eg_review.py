#!/usr/bin/env python3
"""pk-endgame's review: read the journal, score the dry run (or live run).

    python3 crates/pk-endgame/eg_review.py data/state/endgame/journal [--since 2026-11-25T00:00:00Z]

Reports, for the settled positions:
- net ¢ per share after fees, with a standard error clustered by window
  (positions in one window share its outcome), and t = mean / s.e.;
- the fill rate and what the delay cost (paper fill against the instant fill);
- calibration: the model's probability against how often the side won;
- by seconds before the close and by asset;
- our TWAP outcome against Polymarket's (should be ≥ 99%).

The gate before going live: ≥ 150 settled positions over ≥ 3 days including
US hours, net ¢/share > 0 with t ≥ 2, agreement ≥ 99%.
"""
import argparse
import collections
import datetime
import glob
import json
import math
import os


def iso(s):
    return datetime.datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=datetime.timezone.utc).timestamp()


def clustered(rows):
    """Share-weighted ¢/share and its window-clustered s.e."""
    q = sum(r["shares"] for r in rows)
    if q <= 0:
        return float("nan"), float("nan"), 0
    mean = sum(r["pnl"] for r in rows) / q
    by = collections.defaultdict(float)
    for r in rows:
        by[(r["asset"], r["open"])] += r["pnl"] - mean * r["shares"]
    se = math.sqrt(sum(v * v for v in by.values())) / q if len(by) > 1 else float("nan")
    return 100 * mean, 100 * se, len(by)


def line(label, rows):
    m, se, n = clustered(rows)
    # Under 20 windows the clustered s.e. means little (all wins give s.e. ≈ 0).
    t = m / se if n >= 20 and se and se == se and se > 0 else float("nan")
    pnl = sum(r["pnl"] for r in rows)
    print(f"  {label:<22} n={len(rows):>4}  windows={n:>4}  net {m:+7.2f}¢/sh  s.e. {se:5.2f}  t {t:+5.2f}  P&L ${pnl:+8.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal", help="the journal directory")
    ap.add_argument("--since", help="ISO UTC; earlier records are skipped")
    ap.add_argument("--epoch", help="only this epoch's decisions and positions (records without one are epoch 1)")
    a = ap.parse_args()
    since = iso(a.since) if a.since else 0
    recs = []
    for f in sorted(glob.glob(os.path.join(a.journal, "*.jsonl"))):
        for l in open(f):
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("t_ns", 0) / 1e9 >= since:
                recs.append(r)
    by = collections.defaultdict(list)
    for r in recs:
        by[r["type"]].append(r)
    ep = lambda r: r.get("epoch", "1")
    if a.epoch:
        by["eg_decision"] = [r for r in by["eg_decision"] if ep(r) == a.epoch]
        by["eg_settle"] = [r for r in by["eg_settle"] if ep(r) == a.epoch]
        ids = {r["id"] for r in by["eg_decision"]}
        by["eg_fill"] = [r for r in by["eg_fill"] if r["id"] in ids]
        by["eg_no_fill"] = [r for r in by["eg_no_fill"] if r["id"] in ids]
        print(f"epoch {a.epoch}")
    dec = {r["id"]: r for r in by["eg_decision"]}
    fills = {r["id"]: r for r in by["eg_fill"]}
    settles = by["eg_settle"]
    print(f"records {len(recs)}  windows {len(by['eg_window'])}  decisions {len(dec)}  "
          f"fills {len(fills)}  no-fills {len(by['eg_no_fill'])}  settled {len(settles)}")
    if dec:
        print(f"fill rate {len(fills) / len(dec):.0%}")
        filled_sh = sum(f["shares"] for f in fills.values())
        inst_sh = sum(d["instant_shares"] for d in dec.values())
        if inst_sh:
            print(f"shares filled after the delay / at the decision: {filled_sh:.0f} / {inst_sh:.0f} "
                  f"({filled_sh / inst_sh:.0%})")
    res = by["eg_resolution"]
    both = [r for r in res if r.get("ours")]
    if both:
        agree = sum(1 for r in both if r["agree"])
        print(f"our TWAP vs Polymarket: {agree}/{len(both)} agree ({agree / len(both):.2%})")
    if not settles:
        print("nothing settled yet")
        return
    print("\nsettled positions")
    line("all", settles)
    for live in (False, True):
        rows = [s for s in settles if s.get("live") == live]
        if rows and len({s.get("live") for s in settles}) > 1:
            line("live" if live else "paper", rows)
    print("\nby seconds before the close (at the decision)")
    buckets = [(0, 10), (10, 20), (20, 30), (30, 45), (45, 61), (61, 601)]
    for lo, hi in buckets:
        rows = [s for s in settles if s["id"] in dec and lo <= dec[s["id"]]["left"] < hi]
        if rows:
            line(f"{lo}-{hi} s", rows)
    print("\nby asset")
    for asset in sorted({s["asset"] for s in settles}):
        line(asset, [s for s in settles if s["asset"] == asset])
    print("\nby ask paid")
    for lo, hi in [(0, 0.5), (0.5, 0.8), (0.8, 0.9), (0.9, 1.01)]:
        rows = [s for s in settles if s["id"] in fills and lo <= (fills[s["id"]].get("avg") or 0) < hi]
        if rows:
            line(f"{lo:.2f}-{hi:.2f}", rows)
    print("\ncalibration (model P(side) at the decision → how often the side won)")
    for lo, hi in [(0.5, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.01)]:
        rows = [s for s in settles if s["id"] in dec and lo <= dec[s["id"]]["p_side"] < hi]
        if rows:
            won = sum(1 for s in rows if s["won"])
            p = sum(dec[s["id"]]["p_side"] for s in rows) / len(rows)
            ask = sum(fills[s["id"]]["avg"] for s in rows if s["id"] in fills) / len(rows)
            print(f"  P {lo:.2f}-{hi:.2f}  n={len(rows):>4}  model {p:.3f}  won {won / len(rows):.3f}  avg paid {ask:.3f}")
    halts = by["eg_halt"]
    if halts:
        print(f"\nhalts: {[h['day'] for h in halts]}")


if __name__ == "__main__":
    main()
