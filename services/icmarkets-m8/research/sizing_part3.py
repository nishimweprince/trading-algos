"""Follow-up to sizing_exits.py: is the streak ladder's edge real, and does a wide-stop pyramid work?

A  permutation test: shuffle the replayed trade order 2000 times; compare each sizing rule's ret/DD on the real order with
   the shuffled distribution (if trade outcomes cluster, the ladder beats its shuffled self; if not, it is luck).
B  ladder variants vs fixed risk at the same AVERAGE risk deployed (a fair comparison).
C  wide-stop pyramid: probe p% (stop 5 ATR); at +a ATR the probe's stop goes to entry and an add opens with its OWN
   5 ATR stop sized at (5 - p)%; both legs then follow the live rules (break-even at +4 ATR from their own entry,
   reverse on the next signal, Boom TP 2R from the probe entry). Also "confirmation only" (p = 0).
Usage (VPS): python sizing_part3.py > sizing_part3.txt
"""
import numpy as np
from sizing_exits import STRATS, SPLIT_MS, build_trades, leg_path, run_exit, first, trade_R, equity_path, BIG

RNG = np.random.default_rng(7)


def ladder_pcts(R, ladder, mode='reset'):
    out = []; k = 0
    for r in R:
        out.append(ladder[k] * r)
        if r > 0.05:
            k = min(k + 1, len(ladder) - 1)
        elif r < -0.3:
            k = 0 if mode == 'reset' else max(k - 1, 0)
    return np.array(out)


def ladder_risk(R, ladder, mode='reset'):
    out = []; k = 0
    for r in R:
        out.append(ladder[k])
        if r > 0.05:
            k = min(k + 1, len(ladder) - 1)
        elif r < -0.3:
            k = 0 if mode == 'reset' else max(k - 1, 0)
    return np.array(out)


def rdd(p):
    fin, dd = equity_path(p)
    return (fin - 1) / dd if dd else 0.0, fin, dd


def pyramid(t, probe, a_atr, total=5.0):
    side = t['side']; e, px, ent = leg_path(t, side)
    if len(px) == 0:
        return 0.0, False
    atr = t['atr']; S = 5.0 * atr; fav = side * (px - e); n = len(fav)
    tp = 2.0 * S / atr if t['sym'] == 'Boom1000' else None
    j = first(fav >= a_atr * atr)
    i_sl = first(fav <= -S); i_tp = first(fav >= tp * atr) if tp else BIG
    if j == BIG or min(i_sl, i_tp) < j:
        i, _ = run_exit(fav, S, atr, 4.0, tp)
        return probe * fav[i] / S, False
    # probe: stop to entry from j on (or its own BE at +4 ATR, whichever applies) -> exit when fav <= 0 after j, TP, or end
    ip, _ = run_exit(fav, S, atr, None, tp, start=j + 1, floor=0.0)
    e2 = ent[j]; fav2 = side * (px[j + 1:] - e2)
    tp2 = (tp * atr - side * (e2 - e)) / atr if tp else None           # same TP price as the probe
    if len(fav2) == 0:
        return probe * fav[ip] / S, True
    i2, _ = run_exit(fav2, S, atr, 4.0, tp2)
    return probe * fav[ip] / S + (total - probe) * fav2[i2] / S, True


def main():
    T = []
    for s in STRATS:
        T += build_trades(s)
    T.sort(key=lambda t: t['ms0'])
    R = np.array([trade_R(t, t['side'], 5.0, 4.0, 10.0 if t['sym'] == 'Boom1000' else None, None, None)[0] for t in T])
    tr = np.array([t['ms0'] < SPLIT_MS for t in T])
    lad = {'ladder 1-2-3-4-5 reset': ([1, 2, 3, 4, 5], 'reset'), 'ladder 1-3-5 reset': ([1, 3, 5], 'reset'),
           'ladder 1-2-3-4-5 step down': ([1, 2, 3, 4, 5], 'step'), 'ladder 1-5 reset': ([1, 5], 'reset')}

    # lag-1 dependence of outcomes
    w = (R > 0.05).astype(float)
    print(f'P(win) {w.mean():.3f}; P(win | prev win) {w[1:][w[:-1] == 1].mean():.3f}; P(win | prev loss) {w[1:][w[:-1] == 0].mean():.3f}; '
          f'lag-1 corr of R {np.corrcoef(R[:-1], R[1:])[0, 1]:+.3f}')
    for sym in ('Jump50', 'Boom1000'):
        m = np.array([t['sym'] == sym for t in T]); r = R[m]; ww = (r > 0.05).astype(float)
        print(f'  {sym}: P(win) {ww.mean():.3f}, after win {ww[1:][ww[:-1] == 1].mean():.3f}, after loss {ww[1:][ww[:-1] == 0].mean():.3f}')

    for part, mask in (('ALL', np.ones(len(T), bool)), ('train', tr), ('test', ~tr)):
        r = R[mask]
        print(f'\n=== {part}: {len(r)} trades')
        print(f"  {'rule':32s} {'avg risk':>8s} {'final':>8s} {'maxDD':>6s} {'ret/DD':>7s} | fixed@avg: {'final':>7s} {'maxDD':>6s} {'ret/DD':>7s} | shuffled ret/DD p50  P(real>shuf)")
        for k in (1, 2, 3, 5):
            x, fin, dd = rdd(k * r)
            print(f"  {'fixed ' + str(k) + '%':32s} {k:8.2f} {fin:8.3f} {100 * dd:5.1f}% {x:+7.2f}")
        for name, (L, md) in lad.items():
            x, fin, dd = rdd(ladder_pcts(r, L, md)); avg = ladder_risk(r, L, md).mean()
            xf, ff, df = rdd(avg * r)
            sh = np.array([rdd(ladder_pcts(RNG.permutation(r), L, md))[0] for _ in range(2000 if part == 'ALL' else 1000)])
            print(f"  {name:32s} {avg:8.2f} {fin:8.3f} {100 * dd:5.1f}% {x:+7.2f} | {ff:17.3f} {100 * df:5.1f}% {xf:+7.2f} | {np.median(sh):+8.2f}  {np.mean(x > sh):.3f}")

    print('\n=== C: wide-stop pyramid (probe p%, add (5-p)% with its own 5 ATR stop at +a ATR; total open risk <= 5%)')
    for part, sel in (('train', lambda t: t['ms0'] < SPLIT_MS), ('test', lambda t: t['ms0'] >= SPLIT_MS)):
        TT = [t for t in T if sel(t)]
        for probe in (0.0, 1.0, 2.0):
            for a in (1.0, 2.0, 3.0, 4.0):
                out = [pyramid(t, probe, a) for t in TT]; p = np.array([o[0] for o in out]); adds = sum(o[1] for o in out)
                x, fin, dd = rdd(p)
                print(f'  {part:5s} probe {probe:.0f}% add @ +{a:g} ATR [adds {adds}/{len(TT)}]  final x{fin:7.3f}  maxDD {100 * dd:5.1f}%  ret/DD {x:+6.2f}  mean %/trade {p.mean():+.3f}')


if __name__ == '__main__':
    main()
