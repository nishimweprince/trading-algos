#!/usr/bin/env python3
"""pkm's performance review: settled windows from the state file, fills from
the journal. Standard errors are clustered by window (a window's fills share
one outcome), so a t near 2 is the bar for calling an edge.

    journalctl -u pkm -o cat | python3 scripts/pkm_review.py data/state/state.json --since 2026-10-27T08:45:00Z

--since scores only windows that opened at or after that time (one config
regime at a time). The verdict needs about 350 windows: at the 10-27 window
sd of $2.35, that detects $0.25 a window at t = 2.
"""
import argparse
import collections
import datetime
import json
import math
import re
import statistics
import sys

TARGET_WINDOWS = 350
FILL = re.compile(r"\[pkm (\S+)Z\] fill (\S+) (up|down) ([\d.]+) @ ([\d.]+) net (\S+) age_ms (\d+)")
# Kalshi's move in the second before a fill, signed in its favour (cents); and
# the 5 s markout line the engine writes after each fill.
KMOVE = re.compile(r" kmove_c (\S+)")
# Spot's (Coinbase) move likewise, in bp, since the spot guard (on fills and markout5 lines).
SMOVE = re.compile(r" smove_bps (\S+)")
MARKOUT5 = re.compile(r"\] markout5 (\S+) (up|down) .* m5_c (\S+) kmove_c (\S+)")
MOVE_BUCKETS = (("Kalshi against (≤ −0.5¢)", lambda c: c <= -0.5), ("Kalshi flat", lambda c: -0.5 < c < 0.5),
                ("Kalshi with (≥ +0.5¢)", lambda c: c >= 0.5))
SPOT_BUCKETS = (("spot against (≤ −2 bp)", lambda b: b <= -2), ("spot flat", lambda b: -2 < b < 2),
                ("spot with (≥ +2 bp)", lambda b: b >= 2))
# Spot's 30 s and 60 s trend before a fill, signed likewise (bp): the slow
# moves one-sided windows lose to, past the 1 s guard.
TREND = {30: re.compile(r" strend30_bps (\S+)"), 60: re.compile(r" strend60_bps (\S+)")}
TREND_BUCKETS = (("against (≤ −5 bp)", lambda b: b <= -5), ("flat", lambda b: -5 < b < 5), ("with (≥ +5 bp)", lambda b: b >= 5))


def unix(iso):
    return datetime.datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=datetime.timezone.utc).timestamp()


def clustered(rows, key="k"):
    """Share-weighted mean ¢/share of `r` with its window-clustered standard error."""
    q = sum(r["q"] for r in rows)
    if q <= 0:
        return 0.0, 0.0, 0.0
    mean = sum(r["q"] * r["r"] for r in rows) / q
    by = collections.defaultdict(float)
    for r in rows:
        by[r[key]] += r["q"] * (r["r"] - mean)
    return 100 * mean, 100 * math.sqrt(sum(v * v for v in by.values())) / q, q * mean


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state")
    ap.add_argument("--since", help="ISO UTC time; windows opening earlier are skipped")
    args = ap.parse_args()
    since = unix(args.since) if args.since else 0
    inv = json.load(open(args.state))["inventories"]
    settled = {k: v for k, v in inv.items() if v["settled"] and v["open_unix"] >= since}

    pnl = [v["pnl"] for v in settled.values()]
    print(f"windows settled: {len(pnl)} of the {TARGET_WINDOWS} a verdict needs")
    if len(pnl) < 2:
        return
    mean, sd = statistics.mean(pnl), statistics.pstdev(pnl)
    t = mean / (sd / math.sqrt(len(pnl))) if sd > 0 else 0.0
    print(f"realised ${sum(pnl):+.2f}: ${mean:+.3f} a window, sd ${sd:.2f}, t {t:+.2f}")
    one = [v for v in settled.values() if v["up"] < 1e-9 or v["down"] < 1e-9]
    two = [v for v in settled.values() if v not in one]
    print(f"two-sided {len(two)}: ${sum(v['pnl'] for v in two):+.2f}   "
          f"one-sided {len(one)}: ${sum(v['pnl'] for v in one):+.2f} ({sum(v['pnl'] > 0 for v in one)} won)")
    heavy = [(v["up"] > v["down"]) == (v["settled"] == "yes") for v in settled.values() if abs(v["up"] - v["down"]) > 1e-6]
    if heavy:
        print(f"heavier side won {sum(heavy)} of {len(heavy)} ({100 * sum(heavy) / len(heavy):.0f}%)")

    rows, marks = [], []
    for line in sys.stdin if not sys.stdin.isatty() else []:
        mo = MARKOUT5.search(line)
        if mo and mo.group(1) in settled:
            sm = SMOVE.search(line)
            marks.append((float(mo.group(4)), float(mo.group(3)), float(sm.group(1)) if sm else None))
            continue
        m = FILL.search(line)
        if not m or m.group(2) not in settled:
            continue
        v = settled[m.group(2)]
        up, q, px, net = m.group(3) == "up", float(m.group(4)), float(m.group(5)), float(m.group(6))
        before = net - (q if up else -q)
        km = KMOVE.search(line)
        rows.append(dict(k=m.group(2), asset=v["asset"], s=unix(m.group(1)) - v["open_unix"], q=q, px=px,
                         age=int(m.group(7)), opens=(before >= -1e-9) if up else (before <= 1e-9),
                         kmove=float(km.group(1)) if km else None,
                         smove=float(SMOVE.search(line).group(1)) if SMOVE.search(line) else None,
                         **{f"trend{n}": float(m.group(1)) if (m := rx.search(line)) else None for n, rx in TREND.items()},
                         r=(1.0 if (v["settled"] == "yes") == up else 0.0) - px))
    if not rows:
        print("(no fills on stdin: pipe `journalctl -u pkm -o cat` for the per-fill breakdown)")
        return
    buckets = [
        ("all", lambda r: True),
        *[(f"asset {a}", lambda r, a=a: r["asset"] == a) for a in sorted({r["asset"] for r in rows})],
        ("price < 0.35", lambda r: r["px"] < 0.35),
        ("price 0.35-0.65", lambda r: 0.35 <= r["px"] < 0.65),
        ("price >= 0.65", lambda r: r["px"] >= 0.65),
        ("quote age < 1 s", lambda r: r["age"] < 1000),
        ("quote age 1-5 s", lambda r: 1000 <= r["age"] < 5000),
        ("quote age >= 5 s", lambda r: r["age"] >= 5000),
        ("second < 120", lambda r: r["s"] < 120),
        ("second >= 120", lambda r: r["s"] >= 120),
        *[(name, lambda r, sel=sel: r["kmove"] is not None and sel(r["kmove"])) for name, sel in MOVE_BUCKETS],
        *[(name, lambda r, sel=sel: r["smove"] is not None and sel(r["smove"])) for name, sel in SPOT_BUCKETS],
        *[(f"60 s trend {name}", lambda r, sel=sel: r["trend60"] is not None and sel(r["trend60"])) for name, sel in TREND_BUCKETS],
        *[(f"30 s trend {name}", lambda r, sel=sel: r["trend30"] is not None and sel(r["trend30"])) for name, sel in TREND_BUCKETS],
    ]
    for leg, pick in (("first legs", lambda r: r["opens"]), ("pairing legs", lambda r: not r["opens"])):
        print(leg)
        for name, sel in buckets:
            xs = [r for r in rows if pick(r) and sel(r)]
            if xs:
                c, se, usd = clustered(xs)
                print(f"  {name:30s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  ${usd:+8.2f}")
    if marks:
        print("5 s markout vs Kalshi's fair, by the move before the fill")
        for name, sel, idx in [(n, s, 0) for n, s in MOVE_BUCKETS] + [(n, s, 2) for n, s in SPOT_BUCKETS]:
            xs = [m[1] for m in marks if m[idx] is not None and sel(m[idx])]
            if len(xs) > 1:
                se = statistics.pstdev(xs) / math.sqrt(len(xs))
                print(f"  {name:30s} fills {len(xs):4d}  {statistics.mean(xs):+6.2f}¢ ± {se:5.2f}")


if __name__ == "__main__":
    main()
