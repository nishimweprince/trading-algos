#!/usr/bin/env python3
"""pk-endgame's weekly re-fit: how wide should the model's uncertainty be?

    python3 eg_refit.py <state>/journal [--days 7] [--out <state>/refit.json]

For every second in the per-second log (`eg_sec`) with a model value and a
resolved window, it takes the model's z-score with the multiplier that was in
force removed, and finds the sd multiplier per seconds-left band that
minimises log-loss against Polymarket's outcome. It REPORTS only: adopting a
new multiplier is a rule change and starts a new epoch (PREREG.md).
"""
import argparse
import datetime
import glob
import json
import math
import os
import time

BANDS = [("0-30 s", 0, 30), ("30-61 s", 30, 61), ("61-91 s", 61, 91)]
GRID = [0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.5, 1.65, 1.8, 2.0, 2.25, 2.5, 3.0]


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def logloss(c, rows):
    s = 0.0
    for z, y in rows:
        p = min(max(phi(z / c), 1e-6), 1 - 1e-6)
        s -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return s / len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--out")
    a = ap.parse_args()
    since_ns = (time.time() - a.days * 86400) * 1e9
    res, secs = {}, []
    for f in sorted(glob.glob(os.path.join(a.journal, "*.jsonl"))):
        for line in open(f):
            if '"eg_sec"' not in line and '"eg_resolution"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["type"] == "eg_resolution":
                res[(r["asset"], r["open"])] = 1 if r["poly"] == "yes" else 0
            elif r["type"] == "eg_sec" and r.get("t_ns", 0) >= since_ns and "p_up" in r.get("model", {}):
                secs.append(r)
    rows = {label: [] for label, _, _ in BANDS}
    current = {}
    for r in secs:
        y = res.get((r["asset"], r["open"]))
        m = r["model"]
        if y is None or m["sd"] <= 0 or m["mean"] <= 0 or m["k"] <= 0:
            continue
        mult = m.get("mult", 1.0)
        z_base = math.log(m["mean"] / m["k"]) / (m["sd"] / mult)
        for label, lo, hi in BANDS:
            if lo <= r["left"] < hi:
                rows[label].append((z_base, y))
                current.setdefault(label, mult)
    out = {"t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "days": a.days, "seconds": sum(len(v) for v in rows.values()), "bands": []}
    print(f"re-fit over the last {a.days:g} days: {out['seconds']} scored seconds")
    for label, _, _ in BANDS:
        sub = rows[label]
        if len(sub) < 200:
            print(f"  {label}: {len(sub)} seconds, too few")
            continue
        cur = current.get(label, 1.0)
        best = min(GRID, key=lambda c: logloss(c, sub))
        lc, lb = logloss(cur, sub), logloss(best, sub)
        out["bands"].append({"label": label, "n": len(sub), "mult": best, "in_force": cur,
                             "logloss_in_force": round(lc, 5), "logloss_best": round(lb, 5)})
        print(f"  {label}: n={len(sub)}  in force ×{cur} log-loss {lc:.4f}  best ×{best} log-loss {lb:.4f}")
    if a.out:
        tmp = a.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=2)
        os.replace(tmp, a.out)


if __name__ == "__main__":
    main()
