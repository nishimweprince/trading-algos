"""Staircase pyramid sizing for ic_m8 (owner's spec, 2026-11-28), from the account's current equity.

Per trade: probe p0% risk with the live 5 ATR stop. Every time the favourable move reaches another d ATR step:
  step 1: stop of the whole position -> original entry (break-even); add leg 1 sized so a return to that stop loses a1%
  step k: stop -> the level of step k-1 (staircase trail); add leg k (a_k%) with its stop at that new level, while adds last
After the adds run out the staircase keeps trailing ('stair'), or the stop stays at break-even ('be'). Every leg exits
together (netting account: one position, one stop) at the stop, or at the next signal (live reverse). No take profit.
Adds fill at the entry-side price of the trigger tick (ask for longs); stops fill at the tick that crosses them, so
Jump50 jumps through a tight stop cost what they cost.

Dollar simulation: IC 0.1-lot minimum and 0.1 step, $1 per 1.0 price per lot. A leg whose minimum lot would risk more
than 10% of equity (live IC_MAX_RISK_PCT) is skipped (for the probe: the whole trade is skipped). Legs are sized
on the equity at the trade's open.
Reports: the full 86-day history in order (train before 2026-10-15, test after), and a 30-day bootstrap from the
current equity (days resampled with replacement, 2000 runs).
Usage (VPS): python sizing_pyramid.py <current equity>
"""
import sys
import numpy as np
from sizing_exits import STRATS, SPLIT_MS, build_trades, leg_path, run_exit, first, BIG

CAP = 10.0
EQ0 = float(sys.argv[1]) if len(sys.argv) > 1 else 214.77
RNG = np.random.default_rng(11)


def pyramid_legs(t, p0, adds, d, mode):
    """Legs [(risk %, stop distance in price, pnl in price per unit)] and close index for one trade."""
    side = t['side']; e0, px, ent = leg_path(t, side)
    if len(px) == 0:
        return [], 0
    atr = t['atr']; S = 5.0 * atr; fav = side * (px - e0)
    legs = [[p0, S, e0]]; stop = -S; pos = 0; k = 0
    while True:
        i_stop = first(fav <= stop, pos)
        moving = mode == 'stair' or k < max(len(adds), 1)
        j = first(fav >= (k + 1) * d * atr, pos) if moving else BIG
        if i_stop <= j:
            i = min(i_stop, len(fav) - 1); break
        if j == BIG:
            i = len(fav) - 1; break
        k += 1
        stop = 0.0 if k == 1 else (k - 1) * d * atr
        if k <= len(adds):
            dist = side * (ent[j] - e0) - stop
            if dist > 0:
                legs.append([adds[k - 1], dist, ent[j]])
        pos = j + 1
    return [(r, dist, side * (px[i] - e)) for r, dist, e in legs], i


def live_legs(t, pct):
    """Live exits: stop 5 ATR, break-even at +4 ATR, Boom TP 2R, reverse on the next signal."""
    e, px, _ = leg_path(t, t['side'])
    if len(px) == 0:
        return [], 0
    S = 5.0 * t['atr']; fav = t['side'] * (px - e)
    i, _ = run_exit(fav, S, t['atr'], 4.0, 10.0 if t['sym'] == 'Boom1000' else None)
    return [(pct, S, fav[i])], i


def leg_lots(eq, pct, dist):
    l = np.floor(eq * pct / 100 / dist / 0.1 + 1e-9) * 0.1
    if l < 0.1:
        l = 0.1 if 0.1 * dist <= eq * CAP / 100 else 0.0
    return l


def trade_usd(eq, legs):
    if not legs:
        return 0.0, False
    l0 = leg_lots(eq, legs[0][0], legs[0][1])
    if l0 == 0:
        return 0.0, False
    usd = l0 * legs[0][2]
    for r, dist, pnl in legs[1:]:
        usd += leg_lots(eq, r, dist) * pnl
    return usd, True


def run_seq(recs, kind, eq0=EQ0):
    """recs: [(sym, legs_by_rule)] in entry order; kind 'fixed' (legs fixed) or 'ladder' (rung per strategy).
    Sequential (each trade sized on equity after the previous close), the same scheme as the bootstrap."""
    eq = eq0; peak = eq0; mdd = 0.0; n = 0; rung = {'Jump50': 0, 'Boom1000': 0}; LAD = [1, 2, 3, 4, 5]
    for sym, L in recs:
        legs = L(LAD[min(rung[sym], 4)]) if kind == 'ladder' else L
        usd, took = trade_usd(eq, legs)
        if not took:
            continue
        n += 1; eq += usd; peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
        if kind == 'ladder':
            r = legs[0][2] / legs[0][1]
            rung[sym] = rung[sym] + 1 if r > 0.05 else (0 if r < -0.3 else rung[sym])
        if eq <= 1:
            return max(eq, 0.0), 1.0, n
    return eq, mdd, n


def main():
    T = []
    for s in STRATS:
        T += build_trades(s)
    T.sort(key=lambda t: t['ms0'])
    day = np.array([t['ms0'] // 86400000 for t in T]); days = np.unique(day)
    tr = np.array([t['ms0'] < SPLIT_MS for t in T])
    print(f'{len(T)} trades over {len(days)} days; start equity ${EQ0:.2f}; min-lot cap {CAP:g}%')

    rules = {}
    rules['fixed 2% (live exits)'] = ('fixed', [live_legs(t, 2.0)[0] for t in T])
    rules['fixed 5% (live exits)'] = ('fixed', [live_legs(t, 5.0)[0] for t in T])
    live_R = [live_legs(t, 1.0)[0] for t in T]
    rules['LIVE ladder 1-2-3-4-5'] = ('ladder', [(lambda L: (lambda p: [(p, L[0][1], L[0][2])]))(L) for L in live_R])
    combos = [(2, [3]), (2, [3, 3]), (2, [3, 3, 3]), (2, [2]), (2, [1]), (2, [2, 2]), (1, [2, 2]), (1, [4]), (1, [2, 2, 2]),
              (2, []), (5, [])]
    for p0, adds in combos:
        for d in (1, 2, 3, 4):
            for mode in ('stair', 'be'):
                if not adds and mode == 'be':
                    continue
                name = f"{p0}%{''.join('+' + str(a) for a in adds) or ' only'} @ {d} ATR {mode}"
                rules[name] = ('fixed', [pyramid_legs(t, p0, adds, d, mode)[0] for t in T])

    def seq(kind, legs, mask):
        recs = [(T[i]['sym'], legs[i]) for i in np.flatnonzero(mask)]
        if kind == 'ladder':
            recs = [(s, L) for s, L in recs]
        return run_seq(recs, kind)

    def boot(kind, legs, n_days=30, runs=2000):
        by_day = [np.flatnonzero(day == d_) for d_ in days]
        fin = np.empty(runs); dd = np.empty(runs)
        for r in range(runs):
            pick = RNG.integers(0, len(days), n_days)
            idx = np.concatenate([by_day[p] for p in pick])
            fin[r], dd[r], _ = run_seq([(T[i]['sym'], legs[i]) for i in idx], kind)
        return fin, dd

    out = []
    for name, (kind, legs) in rules.items():
        a = seq(kind, legs, np.ones(len(T), bool)); b = seq(kind, legs, tr); c = seq(kind, legs, ~tr)
        out.append((name, kind, legs, a, b, c))
    print(f"\n=== Full history in order, sequential, from ${EQ0:.2f}")
    print(f"  {'rule':34s} {'ALL final':>11s} {'maxDD':>6s} | {'train final':>11s} {'maxDD':>6s} | {'test final':>11s} {'maxDD':>6s}")
    for name, kind, legs, a, b, c in out:
        print(f"  {name:34s} ${a[0]:10.2f} {100 * a[1]:5.1f}% | ${b[0]:10.2f} {100 * b[1]:5.1f}% | ${c[0]:10.2f} {100 * c[1]:5.1f}%")

    print(f"\n=== 30-day bootstrap from ${EQ0:.2f} (2000 runs, days resampled)")
    print(f"  {'rule':34s} {'p10':>9s} {'median':>9s} {'p90':>10s} {'P(loss)':>8s} {'P(-50%)':>8s} {'medDD':>6s} {'P(DD>50%)':>9s}")
    for name, kind, legs, *_ in out:
        fin, dd = boot(kind, legs)
        print(f"  {name:34s} ${np.percentile(fin, 10):8.2f} ${np.median(fin):8.2f} ${np.percentile(fin, 90):9.2f} "
              f"{np.mean(fin < EQ0):7.0%} {np.mean(fin < EQ0 / 2):7.0%} {100 * np.median(dd):5.1f}% {np.mean(dd > 0.5):8.0%}")

    # how the user's exact spec behaves trade by trade
    for d in (1, 2, 3, 4):
        L = [pyramid_legs(t, 2, [3], d, 'stair')[0] for t in T]
        added = [l for l in L if len(l) > 1]
        add_r = np.array([l[1][2] / l[1][1] for l in added])
        print(f"\n2%+3% @ {d} ATR stair: add taken in {len(added)}/{len(T)} trades; add leg result in its own risk units: "
              f"mean {add_r.mean():+.2f}, stopped at/through its stop {np.mean(add_r <= -0.95):.0%}, "
              f"worse than -1.5x its risk (jump through the stop) {np.mean(add_r < -1.5):.0%}, worst {add_r.min():+.1f}x")


if __name__ == '__main__':
    main()
