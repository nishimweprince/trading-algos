#!/usr/bin/env python3
# join3.py PROCESSED POSTPACK SHREDSIGS
# lead = helius_time - other_time (positive = other feed earlier), over pump.fun txs Helius saw.
import sys, collections
A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"; IDX = {c: i for i, c in enumerate(A)}
def k8(s):
    n = 0
    for c in s: n = n * 58 + IDX[c]
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return (b"\0" * (len(s) - len(s.lstrip("1"))) + b).rjust(64, b"\0")[:8].hex()
def q(xs, p): return xs[min(len(xs) - 1, int(p * len(xs)))]
def summ(xs):
    xs = sorted(xs)
    if not xs: return "n 0"
    return (f"n {len(xs):7d}  p10 {q(xs,.1):6.1f}  p25 {q(xs,.25):6.1f}  p50 {q(xs,.5):6.1f}  p75 {q(xs,.75):6.1f}  p90 {q(xs,.9):6.1f}  "
            f"earlier {100*sum(1 for x in xs if x>0)/len(xs):5.1f}%  >=10ms {100*sum(1 for x in xs if x>=10)/len(xs):5.1f}%")

sh = {}; slo = shi = None
for l in open(sys.argv[3]):
    p = l.split()
    if len(p) != 4: continue
    pre, bat, k = int(p[0]), int(p[1]), p[3]
    if k not in sh: sh[k] = (pre, bat)
    slo = pre if slo is None or pre < slo else slo; shi = pre if shi is None or pre > shi else shi
pp = {}
for l in open(sys.argv[2]):
    p = l.split()
    if len(p) == 6: pp.setdefault(k8(p[2]), int(p[0]))
lo, hi = slo + 10_000_000, shi - 10_000_000
H = {}; hslot = {}
for l in open(sys.argv[1]):
    p = l.split()
    if len(p) != 3: continue
    t = int(p[0])
    if lo <= t <= hi:
        k = k8(p[2])
        if k not in H: H[k] = t; hslot[k] = int(p[1])
print(f"window {(hi-lo)/60e6:.1f} min, Helius pump.fun txs {len(H)}")
pre = [(H[k] - sh[k][0]) / 1000 for k in H if k in sh]
bat = [(H[k] - sh[k][1]) / 1000 for k in H if k in sh and sh[k][1] > 0]
print(f"shreds coverage {len(pre)}/{len(H)} = {100*len(pre)/max(len(H),1):.2f}%   (batch-complete decodable {len(bat)})")
print("shreds lead, streaming parse (prefix): ", summ(pre))
print("shreds lead, batch deshred:            ", summ(bat))
ppl = [(H[k] - pp[k]) / 1000 for k in H if k in pp]
print(f"postpack coverage {len(ppl)}/{len(H)} = {100*len(ppl)/max(len(H),1):.2f}%")
print("postpack lead:                         ", summ(ppl))
both = [k for k in H if k in pp and k in sh]
print("postpack vs shreds(prefix) on common txs (positive = postpack earlier):", summ([(sh[k][0] - pp[k]) / 1000 for k in both]))
# per-slot: lead of the FIRST pump.fun tx of each slot (the trigger that matters for "see it first")
first = {}
for k, t in H.items():
    s = hslot[k]
    if s not in first or t < H[first[s]]: first[s] = k
fl = [(H[k] - sh[k][0]) / 1000 for k in first.values() if k in sh]
print("shreds lead on each slot's first pump tx:", summ(fl))
