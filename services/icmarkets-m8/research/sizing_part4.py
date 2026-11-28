"""Implementable ladder: per-strategy rung (each strategy's own last closed trade), dollar simulation from $220 with IC's
0.1-lot minimum / 0.1 step ($1 per 1.0 price per lot), events ordered by time (opens use equity at open; P&L lands at close).
min-lot rule: the 0.1 lot is taken if it risks <= cap% of equity (live IC_MAX_RISK_PCT=10), else the trade is skipped.
Usage (VPS): python sizing_part4.py
"""
import numpy as np
from sizing_exits import STRATS, SPLIT_MS, build_trades, leg_path, run_exit

def outcomes(T):
    out = []
    for t in T:
        e, px, _ = leg_path(t, t['side']); S = 5.0 * t['atr']; fav = t['side'] * (px - e)
        i, _ = run_exit(fav, S, t['atr'], 4.0, 10.0 if t['sym'] == 'Boom1000' else None)
        out.append(dict(sym=t['sym'], t0=t['ms0'], t1=int(t['ms'][1:][i]), S=S, pnl_px=float(fav[i]), R=float(fav[i] / S)))
    return out

def sim(O, rule, eq0=220.0, cap=10.0, boom_short=None):
    ev = sorted([(o['t0'], 0, k) for k, o in enumerate(O)] + [(o['t1'], 1, k) for k, o in enumerate(O)], key=lambda x: (x[0], -x[1]))
    eq = eq0; peak = eq0; mdd = 0; rung = {'Jump50': 0, 'Boom1000': 0}; lots = {}; skipped = 0; risks = []
    for ts, kind, k in ev:
        o = O[k]
        if kind == 0:
            pct = rule(rung[o['sym']]); dollars = eq * pct / 100
            l = np.floor(dollars / o['S'] / 0.1 + 1e-9) * 0.1
            if l < 0.1:
                l = 0.1 if 0.1 * o['S'] <= eq * cap / 100 else 0.0
            lots[k] = l; skipped += l == 0
            if l: risks.append(100 * l * o['S'] / eq)
        else:
            l = lots.pop(k, 0)
            if l:
                eq += l * o['pnl_px']; peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
                if o['R'] > 0.05: rung[o['sym']] += 1
                elif o['R'] < -0.3: rung[o['sym']] = 0
            if eq <= 0: return 0, 1, skipped, 0
    return eq, mdd, skipped, np.mean(risks)

T = []
for s in STRATS: T += build_trades(s)
O = outcomes(T)
L = lambda lad: (lambda k: lad[min(k, len(lad) - 1)])
rules = {'fixed 5%': lambda k: 5, 'fixed 2%': lambda k: 2, 'fixed 1%': lambda k: 1,
         'ladder 1-2-3-4-5': L([1, 2, 3, 4, 5]), 'ladder 1-3-5': L([1, 3, 5]), 'ladder 2-3-4-5': L([2, 3, 4, 5])}
for part, sel in (('ALL', lambda o: True), ('train', lambda o: o['t0'] < SPLIT_MS), ('test', lambda o: o['t0'] >= SPLIT_MS)):
    OO = [o for o in O if sel(o)]
    print(f'\n=== {part} ({len(OO)} trades), $220 start, per-strategy rung, 0.1-lot minimum')
    for cap in (10.0, 5.0):
        for name, rule in rules.items():
            eq, dd, sk, avg = sim(OO, rule, cap=cap)
            print(f'  cap {cap:4.1f}%  {name:18s} final ${eq:10.2f}  maxDD {100*dd:5.1f}%  avg actual risk {avg:4.2f}%  skipped {sk}')
