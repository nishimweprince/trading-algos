"""Sweep take-profit at a fixed stop on the cached posts (no scraping, no OpenAI calls).

Runs the follower model for TP = --tp-from .. --tp-to step --tp-step, once with the env
trailing settings and once without trailing, and prints both tables plus a BUY/SELL split.
Run backtest.py first so data/posts_raw.json, classified.jsonl and the prices are cached.

Usage: python sweep_tp.py [--sl 60 --tp-from 60 --tp-to 150 --tp-step 10 --days 7]
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
    ap.add_argument("--sl", type=float, default=60)
    ap.add_argument("--tp-from", type=float, default=60)
    ap.add_argument("--tp-to", type=float, default=150)
    ap.add_argument("--tp-step", type=float, default=10)
    ap.add_argument("--days", type=float, default=7)
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

    base = replace(Rules.from_cfg(), sl=args.sl)
    variants = {
        f"trail {base.trail_start:g}/{base.trail_dist:g}/{base.trail_step:g}": base,
        "no trail": replace(base, trail_start=0),
    }
    tps = []
    tp = args.tp_from
    while tp <= args.tp_to + 1e-9:
        tps.append(tp)
        tp += args.tp_step

    out = []
    for name, rules in variants.items():
        for tp in tps:
            t = run(ann, bars, replace(rules, tp=tp), "follower")
            t = t[t["status"] == "traded"]
            s = stats(t)
            side = t.groupby("side")["pips"].sum()
            reasons = t["reason"].value_counts()
            out.append({
                "variant": name, "sl": args.sl, "tp": tp, "rr": round(tp / args.sl, 2),
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
    df.to_csv(out_dir / f"sweep_tp_sl{args.sl:g}.csv", index=False)
    pd.set_option("display.width", 200)
    for name, g in df.groupby("variant", sort=False):
        print(f"\n== SL {args.sl:g}, {name}, entry delay {CFG.entry_delay_seconds:g}s ==")
        print(g.drop(columns=["variant", "sl"]).to_string(index=False))
        best = g.loc[g["net_pips"].idxmax()]
        print(f"best net: TP {best.tp:g} -> {best.net_pips:+.1f} pips "
              f"(win {best.win_rate:.0%}, t {best.t_stat})")


if __name__ == "__main__":
    main()
