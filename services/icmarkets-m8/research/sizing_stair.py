"""Win ladder (1-2-3-4-5%) combined with a break-even + stair-step stop, from the account's current equity.

Exit grid: stop 5 ATR; at +b ATR the stop goes to entry (break-even), then every further s ATR it steps up to the previous
trigger level (b, b+s, b+2s, ...). b in 1..5, s in 1..5, plus 'BE only' (no stepping). No take profit; the next signal
closes and reverses (live). 'LIVE exits' = BE 4 ATR, Boom TP 2R. Sizing: live ladder (per-strategy rung: +1 after a
win >= 0.05R, back to 1% after a loss <= -0.3R) or fixed 2%. Same dollar model as sizing_pyramid.py.
Usage (VPS): python sizing_stair.py <current equity>
"""
import numpy as np
from sizing_exits import STRATS, SPLIT_MS, build_trades, leg_path, first, BIG
from sizing_pyramid import live_legs, run_seq, EQ0, RNG


def stair_unit(t, b, s):
    """(stop distance, pnl in price) for one unit position; s None = break-even only."""
    side = t['side']; e0, px, _ = leg_path(t, side)
    if len(px) == 0:
        return 5.0 * t['atr'], 0.0
    atr = t['atr']; S = 5.0 * atr; fav = side * (px - e0)
    stop = -S; pos = 0; k = 0
    while True:
        lvl = b if k == 0 else b + k * s if s else BIG
        i_stop = first(fav <= stop, pos)
        j = first(fav >= lvl * atr, pos) if lvl < BIG else BIG
        if i_stop <= j:
            i = min(i_stop, len(fav) - 1); break
        if j == BIG:
            i = len(fav) - 1; break
        stop = 0.0 if k == 0 else (b + (k - 1) * s) * atr
        k += 1; pos = j + 1
    return S, float(fav[i])


def main():
    T = []
    for st in STRATS:
        T += build_trades(st)
    T.sort(key=lambda t: t['ms0'])
    day = np.array([t['ms0'] // 86400000 for t in T]); days = np.unique(day); by_day = [np.flatnonzero(day == d_) for d_ in days]
    tr = np.array([t['ms0'] < SPLIT_MS for t in T])
    print(f'{len(T)} trades over {len(days)} trading days; start equity ${EQ0:.2f}')

    exits = {'LIVE exits (BE 4, Boom TP 2R)': [tuple(live_legs(t, 1.0)[0][0][1:]) for t in T]}
    for b in (1, 2, 3, 4, 5):
        exits[f'BE {b} only'] = [stair_unit(t, b, None) for t in T]
        for s in (1, 2, 3, 4, 5):
            exits[f'BE {b} step {s}'] = [stair_unit(t, b, s) for t in T]

    def legs_for(units, sizing):
        if sizing == 'ladder':
            return 'ladder', [(lambda u: (lambda p: [(p, u[0], u[1])]))(u) for u in units]
        return 'fixed', [[(2.0, u[0], u[1])] for u in units]

    def seq(kind, legs, mask):
        return run_seq([(T[i]['sym'], legs[i]) for i in np.flatnonzero(mask)], kind)

    def boot(kind, legs, runs=2000, n_days=30):
        fin = np.empty(runs); dd = np.empty(runs)
        for r in range(runs):
            idx = np.concatenate([by_day[p] for p in RNG.integers(0, len(days), n_days)])
            fin[r], dd[r], _ = run_seq([(T[i]['sym'], legs[i]) for i in idx], kind)
        return fin, dd

    for sizing in ('ladder', 'fixed 2%'):
        print(f'\n=== {sizing.upper()}: history in order from ${EQ0:.2f} (train < 2026-10-15 <= test) and 30-day bootstrap (2000 runs)')
        print(f"  {'exit':30s} {'R/trade':>8s} | {'train':>9s} {'DD':>5s} | {'test':>9s} {'DD':>5s} | {'p10':>8s} {'median':>8s} {'p90':>9s} {'P(loss)':>7s} {'medDD':>6s} {'P(DD>50%)':>9s}")
        for name, units in exits.items():
            kind, legs = legs_for(units, 'ladder' if sizing == 'ladder' else 'fixed')
            Rm = np.mean([u[1] / u[0] for u in units])
            b = seq(kind, legs, tr); c = seq(kind, legs, ~tr); fin, dd = boot(kind, legs)
            print(f"  {name:30s} {Rm:+8.3f} | ${b[0]:8.2f} {100 * b[1]:4.0f}% | ${c[0]:8.2f} {100 * c[1]:4.0f}% | "
                  f"${np.percentile(fin, 10):7.2f} ${np.median(fin):7.2f} ${np.percentile(fin, 90):8.2f} {np.mean(fin < EQ0):6.0%} "
                  f"{100 * np.median(dd):5.1f}% {np.mean(dd > 0.5):8.0%}", flush=True)


if __name__ == '__main__':
    main()
