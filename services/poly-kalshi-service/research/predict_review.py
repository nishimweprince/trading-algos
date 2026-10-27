#!/usr/bin/env python3
"""pk-predict's performance review from its journal: settled windows, live
fills split into first and pairing legs, and markouts. Standard errors are
clustered by market (one window's fills share one outcome), so a t near 2 is
the bar for calling an edge.

    python3 research/predict_review.py data/state-predict/journal --since 2026-10-27T10:30:00Z

Settlements come from `position_settled` and from the `settled` record a
`redeemed` entry carries (windows redeemed before accounting ran live). Fills
are the venue's own `wallet_event` fill rows. --since scores only windows that
opened at or after that time, so one config regime at a time. Markout rows
carry no live/shadow flag: pass --since to leave out a shadow period.
"""
import argparse
import collections
import datetime
import glob
import json
import math
import os
import re
import statistics

KEEP = ('"type":"position_settled"', '"type":"redeemed"', '"type":"markout"', '"type":"wallet_event"')
WINDOW = re.compile(r"-(\d{10})$")


def unix_ms(iso):
    return datetime.datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000


def rows(journal):
    files = sorted(glob.glob(os.path.join(journal, "*.jsonl"))) if os.path.isdir(journal) else [journal]
    for path in files:
        with open(path) as f:
            for line in f:
                if any(k in line for k in KEEP):
                    yield json.loads(line)


def window_open_ms(label):
    m = WINDOW.search(label or "")
    return int(m.group(1)) * 1000 if m else None


def clustered(xs):
    """Share-weighted mean ¢/share with its market-clustered standard error, and dollars."""
    q = sum(x["q"] for x in xs)
    mean = sum(x["q"] * x["r"] for x in xs) / q
    by = collections.defaultdict(float)
    for x in xs:
        by[x["market"]] += x["q"] * (x["r"] - mean)
    return 100 * mean, 100 * math.sqrt(sum(v * v for v in by.values())) / q, q * mean


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal", help="the journal directory (or one .jsonl file)")
    ap.add_argument("--since", help="ISO UTC time; windows opening earlier are skipped")
    args = ap.parse_args()
    since = unix_ms(args.since) if args.since else 0

    settled, fills, markouts = {}, [], collections.defaultdict(list)
    for x in rows(args.journal):
        kind = x["type"]
        if kind == "position_settled":
            settled[x["market"]] = x
        elif kind == "redeemed" and isinstance(x.get("settled"), dict):
            settled.setdefault(x["settled"]["market"], x["settled"])
        elif kind == "markout" and x["t_ms"] >= since:
            markouts[x["horizon_s"]].append(x["markout"])
        elif kind == "wallet_event" and "fill" in x:
            d = x.get("details", {})
            size = int(x["fill"].get("executedSizeWei", "0")) / 1e18
            if size > 0:
                fills.append(dict(t=x["t_ms"], market=str(d.get("marketId")), label=d.get("categorySlug"),
                                  yes=d.get("outcome") == "YES", q=size,
                                  px=int(x["fill"]["executedPriceWei"]) / 1e18))

    windows = [v for v in settled.values() if (window_open_ms(v.get("label")) or v.get("opened_ms", 0)) >= since]
    print(f"windows settled: {len(windows)}")
    if len(windows) >= 2:
        pnl = [v["realised"] for v in windows]
        mean, sd = statistics.mean(pnl), statistics.pstdev(pnl)
        t = mean / (sd / math.sqrt(len(pnl))) if sd > 0 else 0.0
        print(f"realised ${sum(pnl):+.2f}: ${mean:+.3f} a window, sd ${sd:.2f}, t {t:+.2f}")
        two = [v for v in windows if v["yes_qty"] > 1e-9 and v["no_qty"] > 1e-9]
        one = [v for v in windows if v not in two]
        print(f"two-sided {len(two)}: ${sum(v['realised'] for v in two):+.2f}   "
              f"one-sided {len(one)}: ${sum(v['realised'] for v in one):+.2f} ({sum(v['realised'] > 0 for v in one)} won)")
        heavy = [(v["yes_qty"] > v["no_qty"]) == (v["outcome"] == "YES")
                 for v in windows if abs(v["yes_qty"] - v["no_qty"]) > 1e-6]
        if heavy:
            print(f"heavier side won {sum(heavy)} of {len(heavy)}")

    for h in sorted(markouts):
        xs = markouts[h]
        if len(xs) > 1:
            print(f"markout {h:>3d} s: {100 * statistics.mean(xs):+.2f}¢ ± {100 * statistics.pstdev(xs) / math.sqrt(len(xs)):.2f} (n {len(xs)})")

    outcome = {m: v["outcome"] == "YES" for m, v in settled.items()}
    net = collections.defaultdict(float)
    scored = []
    for f in sorted(fills, key=lambda f: f["t"]):
        before = net[f["market"]]
        net[f["market"]] += f["q"] if f["yes"] else -f["q"]
        if f["market"] not in outcome or (window_open_ms(f["label"]) or 0) < since:
            continue
        opens = before >= -1e-9 if f["yes"] else before <= 1e-9
        scored.append(dict(f, opens=opens, r=(1.0 if outcome[f["market"]] == f["yes"] else 0.0) - f["px"]))
    if not scored:
        return
    buckets = [
        ("all", lambda f: True),
        ("price < 0.35", lambda f: f["px"] < 0.35),
        ("price 0.35-0.65", lambda f: 0.35 <= f["px"] < 0.65),
        ("price >= 0.65", lambda f: f["px"] >= 0.65),
        *[(a.upper(), lambda f, a=a: (f["label"] or "").startswith(a)) for a in ("btc", "eth", "bnb")],
    ]
    for leg, pick in (("first legs", lambda f: f["opens"]), ("pairing legs", lambda f: not f["opens"])):
        print(leg)
        for name, sel in buckets:
            xs = [f for f in scored if pick(f) and sel(f)]
            if xs:
                c, se, usd = clustered(xs)
                print(f"  {name:16s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  ${usd:+7.2f}")


if __name__ == "__main__":
    main()
