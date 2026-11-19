#!/usr/bin/env python3
# pdjoin.py PROCESSED POSTPACK
# PROCESSED: "unix_us slot sig58"                      (Helius LaserStream processed, pump.fun programs)
# POSTPACK:  "unix_us slot sig58 leader58 tag fmt"     (Astralane postpack, same programs)
# lead = processed_time - postpack_time (positive = postpack earlier)
import sys, collections

def q(xs, p):
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")

def summ(xs):
    xs = sorted(xs)
    if not xs: return "n 0"
    return (f"n {len(xs):6d}  p10 {q(xs,.1):6.1f}  p50 {q(xs,.5):6.1f}  p90 {q(xs,.9):6.1f}  "
            f"earlier {100*sum(1 for x in xs if x>0)/len(xs):5.1f}%  >=10ms {100*sum(1 for x in xs if x>=10)/len(xs):5.1f}%")

proc = {}; proc_slot = {}; slot_txs = collections.defaultdict(list)
for line in open(sys.argv[1]):
    p = line.split()
    if len(p) != 3: continue
    t, s, sig = int(p[0]), int(p[1]), p[2]
    if sig not in proc:
        proc[sig] = t; proc_slot[sig] = s; slot_txs[s].append(t)
pp = {}
for line in open(sys.argv[2]):
    p = line.split()
    if len(p) != 6: continue
    t, s, sig, leader, tag, fmt = int(p[0]), int(p[1]), p[2], p[3], p[4], p[5]
    if sig not in pp: pp[sig] = (t, s, leader, tag, fmt)

lo = max(min(proc.values()), min(v[0] for v in pp.values())) + 5_000_000
hi = min(max(proc.values()), max(v[0] for v in pp.values())) - 5_000_000
P = {k: v for k, v in proc.items() if lo <= v <= hi}
Q = {k: v for k, v in pp.items() if lo <= v[0] <= hi}
print(f"window {(hi-lo)/60e6:.1f} min | processed pump.fun txs {len(P)} ({len(P)/((hi-lo)/1e6):.0f}/s) | postpack txs {len(Q)} ({len(Q)/((hi-lo)/1e6):.1f}/s)")

matched = {k: (P[k] - v[0]) / 1000.0 for k, v in Q.items() if k in P}
print(f"coverage: {len(matched)}/{len(P)} = {100*len(matched)/max(len(P),1):.2f}% of processed pump.fun txs were seen by postpack")
print(f"postpack txs never seen processed: {len(Q)-len(matched)} ({100*(len(Q)-len(matched))/max(len(Q),1):.1f}%)")
pslots = {proc_slot[k] for k in P}
cov_slots = {proc_slot[k] for k in matched}
print(f"slots with pump.fun activity {len(pslots)}, slots where postpack saw any of it {len(cov_slots)} ({100*len(cov_slots)/max(len(pslots),1):.1f}%)")
# within covered slots, what share of that slot's txs did postpack catch?
in_cov = sum(1 for k in P if proc_slot[k] in cov_slots)
print(f"inside covered slots: {len(matched)}/{in_cov} = {100*len(matched)/max(in_cov,1):.1f}% of txs caught")
print()
print("lead ms (positive = postpack earlier)")
print("  all      ", summ(list(matched.values())))
by = collections.defaultdict(list)
for k, l in matched.items(): by[Q[k][3]].append(l)
for tag in sorted(by): print(f"  tag {tag}    ", summ(by[tag]))
byf = collections.defaultdict(list)
for k, l in matched.items(): byf[Q[k][4]].append(l)
for f in sorted(byf): print(f"  fmt {f}    ", summ(byf[f]))
# unmatched by fmt (signature parsing check)
um = collections.Counter(v[4] for k, v in Q.items() if k not in P)
al = collections.Counter(v[4] for v in Q.values())
print("  unmatched share by fmt:", {f: f"{um[f]}/{al[f]}" for f in al})
# position in slot: rank of processed arrival within its slot
rank = {}
for s, ts in slot_txs.items():
    ts.sort()
for k in matched:
    ts = slot_txs[proc_slot[k]]
    import bisect
    rank[k] = bisect.bisect_left(ts, P[k]) / max(len(ts), 1)
print("  by position in slot (processed arrival order):")
for a, b in [(0, .2), (.2, .5), (.5, 1.01)]:
    print(f"    {a:.1f}-{min(b,1):.1f}  ", summ([matched[k] for k in matched if a <= rank[k] < b]))
print()
print("by leader (top 12 by matched txs)")
bl = collections.defaultdict(list)
for k, l in matched.items(): bl[Q[k][2]].append(l)
for ld, xs in sorted(bl.items(), key=lambda kv: -len(kv[1]))[:12]:
    xs.sort(); print(f"  {ld[:10]}…  {summ(xs)}")
