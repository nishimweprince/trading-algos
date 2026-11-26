import json, glob, os, collections
import numpy as np
R = {os.path.basename(f)[:-5]: json.load(open(f)) for f in sorted(glob.glob('/opt/dsi-analogs/wf/out/*.json'))}
ORDER = ['CBF150', 'CBF300', 'CBF500', 'CBF1000', 'VSW_L', 'VSW_M', 'VSW_H', 'TREK_U', 'TREK_D', 'RB100', 'RB200',
         'VB750', 'VC750', 'DEX900U', 'DEX900D', 'SKEW5U', 'SKEW5D', 'EXP1', 'SNAP50', 'LADU', 'LADD', 'V75']
fam = lambda k: ''.join(ch for ch in k.split('|')[0] if not ch.isdigit()).rstrip('_') if not k.startswith('carry') else k.split('|')[0]

print('== Walk-forward (train 28 d -> test 7 d, tick fills, 1 s latency) ==')
print('code      days spr_bp sig_bp spikes/d  weeks picked   oos_n  net/trade_bp  t     total_bp  pos/picked')
for c in ORDER:
    if c not in R: print(c, 'missing'); continue
    x = R[c]; s = x['wf_summary']
    print(f"{c:8} {x['days']:4} {x['spread_bps_mean']:6.2f} {x['sigma_bps']:6.2f} {x['n_spikes8']/x['days']:8.0f}  {s['weeks']:5} {s['weeks_with_picks']:6} {s['oos_n']:7} "
          f"{(s['oos_net_per_trade'] or 0):12.3f} {(s['oos_daily_t'] or 0):5.2f} {s['oos_total_bps']:10.1f}  {s['weeks_pos']}/{s['weeks_with_picks']}")

def passes(st, t_key, cons_key, t_min, cons_min, sign=1):
    return st['weeks'] >= 10 and sign * st[t_key] >= t_min and st[cons_key] >= cons_min

print('\n== False-positive calibration ==')
for lab, sel in (('V75 control (all rules)', lambda c, k: c == 'V75'), ('hour-of-day rules, all symbols', lambda c, k: k.startswith('hour'))):
    tot = sum(1 for c in R for k in R[c]['stats'] if sel(c, k))
    for tm in (3, 4):
        npos = sum(1 for c in R for k, st in R[c]['stats'].items() if sel(c, k) and passes(st, 't_net', 'cons_net', tm, .75))
        ng = sum(1 for c in R for k, st in R[c]['stats'].items() if sel(c, k) and (passes(st, 't_gross', 'cons_gross', tm, .75) or passes(st, 't_gross', 'cons_gross', tm, .75, -1)))
        print(f"  {lab}: {tot} rules; net t>={tm} & 75% weeks: {npos}; |gross t|>={tm} & 75% weeks: {ng}")
ntot = sum(len(R[c]['stats']) for c in R)
print(f'  total rules tested: {ntot}')

print('\n== TRADABLE recurring patterns: net t>=4, same sign in >=80% of weeks (full 17 weeks, not walk-forward) ==')
T = [(c, k, st) for c in ORDER if c in R for k, st in R[c]['stats'].items()
     if not k.startswith('hour') and passes(st, 't_net', 'cons_net', 4, .8)]
for c, k, st in sorted(T, key=lambda z: -z[2]['t_net'])[:40]:
    print(f"  {c:8} {k:28} n{st['n']:7} net {st['net']:+8.3f}bp gross {st['gross']:+8.3f}bp t {st['t_net']:5.1f} weeks+ {st['cons_net']:.2f}")
print(f'  ({len(T)} rules)')

print('\n== STRUCTURAL recurring patterns (gross |t|>=5, >=85% weeks) that are NOT tradable (net t<2) ==')
G = [(c, k, st) for c in ORDER if c in R for k, st in R[c]['stats'].items()
     if not k.startswith('hour') and k.split('|')[1] == 'follow' and (passes(st, 't_gross', 'cons_gross', 5, .85) or passes(st, 't_gross', 'cons_gross', 5, .85, -1))
     and st['t_net'] < 2]
bykey = collections.defaultdict(list)
for c, k, st in G:
    bykey[(c, fam(k))].append((k, st))
for (c, f), v in sorted(bykey.items()):
    k, st = max(v, key=lambda z: abs(z[1]['t_gross']))
    d = 'continues' if st['gross'] > 0 else 'reverses'
    print(f"  {c:8} {f:14} {len(v):3} rules; strongest {k:28} gross {st['gross']:+8.3f}bp (t {st['t_gross']:+.1f}, {d}) net {st['net']:+8.3f}bp spread-eaten")

print('\n== Pattern families recurring ACROSS symbols (net t>=4, >=80% weeks) ==')
cnt = collections.defaultdict(set)
for c, k, st in T:
    cnt[(fam(k), k.split('|')[1])].add(c)
for (f, mode), cs in sorted(cnt.items(), key=lambda z: -len(z[1])):
    print(f"  {f:14} {mode:6} on {len(cs)} symbols: {', '.join(sorted(cs))}")

print('\n== Walk-forward picks: how often each rule was chosen, and its OOS ==')
P = collections.defaultdict(list)
for c in ORDER:
    if c not in R: continue
    for w in R[c]['wf']:
        for p in w['picks']:
            if p['oos_n']:
                P[(c, p['rule'])].append((p['oos_n'], p['oos_net']))
rows = []
for (c, rule), v in P.items():
    n = sum(a for a, _ in v); net = sum(a * b for a, b in v) / n
    rows.append((len(v), c, rule, n, net, sum(1 for a, b in v if b > 0)))
for k, c, rule, n, net, pos in sorted(rows, key=lambda z: (-z[0], -z[4]))[:30]:
    print(f"  {c:8} {rule:28} picked {k:2} wks  oos n {n:6}  net {net:+8.3f}bp  wks+ {pos}/{k}")
