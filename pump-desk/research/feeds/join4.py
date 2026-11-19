#!/usr/bin/env python3
# join4.py PROCESSED HPRECONF POSTPACK SHREDSIGS
# Baseline = Helius LaserStream processed (pump-desk's feed). lead = baseline_time - feed_time (positive = feed earlier).
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
            f"earlier {100*sum(1 for x in xs if x>0)/len(xs):5.1f}%  >=10ms {100*sum(1 for x in xs if x>=10)/len(xs):5.1f}%  >=50ms {100*sum(1 for x in xs if x>=50)/len(xs):5.1f}%")

hp = {}; hst = {}; hslots = collections.Counter()
for l in open(sys.argv[2]):
    p = l.split()
    if len(p) == 5:
        k = k8(p[2])
        if k not in hp: hp[k] = int(p[0]); hst[k] = p[3]; hslots[int(p[1])] += 1
pp = {}
for l in open(sys.argv[3]):
    p = l.split()
    if len(p) == 6: pp.setdefault(k8(p[2]), int(p[0]))
sh = {}
for l in open(sys.argv[4]):
    p = l.split()
    if len(p) == 4: sh.setdefault(p[3], int(p[0]))
H = {}; hslot = {}
for l in open(sys.argv[1]):
    p = l.split()
    if len(p) == 3:
        k = k8(p[2])
        if k not in H: H[k] = int(p[0]); hslot[k] = int(p[1])
lo = max(min(H.values()), min(hp.values()), min(sh.values())) + 10_000_000
hi = min(max(H.values()), max(hp.values()), max(sh.values())) - 10_000_000
H = {k: t for k, t in H.items() if lo <= t <= hi}
secs = (hi - lo) / 1e6
slots = {hslot[k] for k in H}
print(f"window {secs/60:.1f} min, baseline (LaserStream processed) pump.fun txs {len(H)} ({len(H)/secs:.0f}/s), slots {len(slots)}")
print()
for name, feed in (("Helius preconf", hp), ("Astralane postpack", pp), ("Astralane shreds", sh)):
    m = {k: (H[k] - feed[k]) / 1000 for k in H if k in feed}
    cs = {hslot[k] for k in m}
    print(f"{name}: coverage {100*len(m)/len(H):.1f}% of txs, {100*len(cs)/len(slots):.1f}% of slots")
    print("   lead ms ", summ(list(m.values())))
    first = {}
    for k in H:
        s = hslot[k]
        if s not in first or H[k] < H[first[s]]: first[s] = k
    fl = [m[k] for k in first.values() if k in m]
    print("   slot's first pump tx ", summ(fl))
    if name == "Helius preconf":
        for s in sorted(set(hst.values())):
            print(f"   status {s}: ", summ([v for k, v in m.items() if hst[k] == s]))
        hw = [k for k, t in hp.items() if lo <= t <= hi]
        extra = sum(1 for k in hw if k not in H)
        print(f"   preconf txs never seen processed: {extra}/{len(hw)} ({100*extra/max(len(hw),1):.1f}%)")
        hs = [c for s, c in hslots.items()]
        cred = sum(max(300, 10 * c) for c in hs)
        tot = sum(hs); dur = (max(hp.values()) - min(hp.values())) / 1e6
        print(f"   messages {tot} over {dur:.0f} s = {tot/dur:.0f}/s; est credits {cred} = {cred*3600/dur/1e6:.1f}M/hour")
    print()
both = [k for k in H if k in hp and k in pp]
print("postpack vs Helius preconf on common txs (positive = postpack earlier):", summ([(hp[k] - pp[k]) / 1000 for k in both]))
both = [k for k in H if k in hp and k in sh]
print("shreds vs Helius preconf on common txs (positive = shreds earlier):  ", summ([(hp[k] - sh[k]) / 1000 for k in both]))
