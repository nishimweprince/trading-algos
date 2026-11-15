"""Sweep stop-loss or take-profit on the cached posts (no scraping, no OpenAI calls).

Runs the follower model for --param (tp or sl) = --from .. --to step --step, holding the
other one fixed, once with the env trailing settings and once without trailing, and prints
both tables plus a BUY/SELL split. Run backtest.py first so data/posts_raw.json,
classified.jsonl and the prices are cached.

Usage:
  python sweep.py --param tp --sl 60 --from 60 --to 150 --step 10
  python sweep.py --param sl --tp 150 --from 30 --to 100 --step 10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import channel
import classify
import pandas as pd
import prices
import signals
from backtest import STALE_PIPS_DEFAULT, Rules, annotate, run, stats
from config import CFG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--param", choices=["tp", "sl"], default="tp")
    ap.add_argument("--sl", type=float, default=60, help="fixed stop when sweeping tp")
    ap.add_argument("--tp", type=float, default=150, help="fixed target when sweeping sl")
    ap.add_argument("--from", dest="start", type=float, default=60)
    ap.add_argument("--to", dest="stop", type=float, default=150)
    ap.add_argument("--step", type=float, default=10)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--start", dest="date_from", help="ideas posted on/after YYYY-MM-DD")
    ap.add_argument("--end", dest="date_to", help="ideas posted before YYYY-MM-DD")
    args = ap.parse_args()

    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)
    raw = json.loads((CFG.data_dir / "posts_raw.json").read_text())
    posts = [channel.Post(**d) for d in raw]
    posts = [p for p in posts if p.ts >= since]
    rows = classify.classify_all(posts, classify.Cache(CFG.data_dir / "classified.jsonl"))
    ideas = [i for i in signals.build(rows) if i.ticker == "XAUUSD"]
    bars = prices.load("XAUUSD", since - timedelta(days=1), now)
    ann = annotate(ideas, bars, STALE_PIPS_DEFAULT)
    if args.date_from:
        ann = ann[ann["posted"] >= pd.Timestamp(args.date_from)]
    if args.date_to:
        ann = ann[ann["posted"] < pd.Timestamp(args.date_to)]
    window = f"{args.date_from or 'start'}..{args.date_to or 'now'}"

    base = replace(Rules.from_cfg(), sl=args.sl, tp=args.tp)
    if base.trail_start <= 0:  # trailing off in env: compare against the old 50/30/10 trail
        base = replace(base, trail_start=50, trail_dist=30, trail_step=10)
    fixed = ("sl", args.sl) if args.param == "tp" else ("tp", args.tp)
    variants = {
        f"trail {base.trail_start:g}/{base.trail_dist:g}/{base.trail_step:g}": base,
        "no trail": replace(base, trail_start=0),
    }
    values = []
    v = args.start
    while v <= args.stop + 1e-9:
        values.append(v)
        v += args.step

    out = []
    for name, rules in variants.items():
        for v in values:
            r = replace(rules, **{args.param: v})
            t = run(ann, bars, r, "follower")
            t = t[t["status"] == "traded"]
            s = stats(t)
            side = t.groupby("side")["pips"].sum()
            reasons = t["reason"].value_counts()
            out.append({
                "variant": name, "sl": r.sl, "tp": r.tp, "rr": round(r.tp / r.sl, 2),
                **{k: s[k] for k in ("trades", "win_rate", "net_pips", "avg_pips", "t_stat")},
                "buy_pips": round(side.get("BUY", 0), 1),
                "sell_pips": round(side.get("SELL", 0), 1),
                "n_tp": int(reasons.get("tp", 0)), "n_sl": int(reasons.get("sl", 0)),
                "n_trail": int(reasons.get("trail", 0)),
                # profit factor: gross wins / gross losses
                "pf": round(t.loc[t.pips > 0, "pips"].sum()
                            / max(1e-9, -t.loc[t.pips < 0, "pips"].sum()), 2),
            })
    df = pd.DataFrame(out)
    out_dir = CFG.data_dir / "results"
    out_dir.mkdir(exist_ok=True)
    windowed = args.date_from or args.date_to
    tag = f"_{args.date_from or ''}_{args.date_to or ''}" if windowed else ""
    df.to_csv(out_dir / f"sweep_{args.param}_{fixed[0]}{fixed[1]:g}{tag}.csv", index=False)
    pd.set_option("display.width", 200)
    for name, g in df.groupby("variant", sort=False):
        print(f"\n== {window}, {fixed[0].upper()} {fixed[1]:g}, {name}, "
              f"entry delay {CFG.entry_delay_seconds:g}s ==")
        print(g.drop(columns=["variant", fixed[0]]).to_string(index=False))
        best = g.loc[g["net_pips"].idxmax()]
        print(f"best net: {args.param.upper()} {best[args.param]:g} -> {best.net_pips:+.1f} pips "
              f"(win {best.win_rate:.0%}, t {best.t_stat})")


if __name__ == "__main__":
    main()
