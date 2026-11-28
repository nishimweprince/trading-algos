"""Exit and position-sizing study for ic_m8 (IC-J50-M1-RS and IC-B1000-M5-TP2) on the IC Markets tick history.

Replays the live signal engine (ic_m8.signals, same parameters) on M1 bid bars built from /opt/dsi-analogs/wf_ic/ticks,
enters at the first tick >= bar close + 2 s (ask for longs, bid for shorts), and runs each trade tick by tick until its
exit or the strategy's next signal (where the live runner closes and reverses).

Part 1  exits, equal 1-unit risk: stop / break-even / take-profit / trailing variants, in R (R = stop distance).
        Control: the same exits with the signal side flipped (if the flipped side earns the same, the entry has no edge).
Part 2  sizing on the trade sequence, compounding from equity 1.0:
        fixed k% risk; streak ladder (anti-martingale across trades); and in-trade scale-in (probe p%, add the rest
        once price has moved a x ATR in favour, with the probe's stop moved to entry first so total open risk <= 5%).
Split: train = before 2026-10-15 (server time), test = after. Variants are chosen on train and reported on test.

Usage (VPS): /opt/trading-algos/services/deriv-jev/.venv/bin/python sizing_exits.py > sizing_exits.txt
"""
import calendar, os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICKS = '/opt/dsi-analogs/wf_ic/ticks'
P = dict(lookback=55, min_move=1.5, hold=3, cooldown=30, stop_atr=5.0, be_atr=4.0, atr_n=14)
SPLIT_MS = calendar.timegm((2026, 10, 15, 0, 0, 0)) * 1000
ENTRY_DELAY_MS = 2000
STRATS = [dict(tag='IC-J50-M1-RS', sym='Jump50', tf=1, tp_r=0.0), dict(tag='IC-B1000-M5-TP2', sym='Boom1000', tf=5, tp_r=2.0)]
BIG = 1 << 60


def wilder_atr(h, l, c, n):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    a = np.full(len(c), np.nan); a[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        a[i] = a[i - 1] + (tr[i] - a[i - 1]) / n
    return a


def signals(c, a, lookback, min_move, hold, cooldown):
    n = len(c); sig = np.zeros(n, np.int64); bull = bear = 0; last_bar = -10 ** 9; last_dir = 0
    for i in range(n):
        if i < lookback or np.isnan(a[i]) or a[i] <= 0:
            continue
        m = c[i] - c[i - lookback]
        d = 1 if m > min_move * a[i] else (-1 if m < -min_move * a[i] else 0)
        bull = bull + 1 if d == 1 else 0; bear = bear + 1 if d == -1 else 0
        can = i - last_bar >= cooldown
        if bull >= hold and can and last_dir != 1:
            sig[i] = 1; last_dir = 1; last_bar = i
        elif bear >= hold and can and last_dir != -1:
            sig[i] = -1; last_dir = -1; last_bar = i
    return sig


def bars(ms, bid, tf):
    g = ms // (60000 * tf)
    st = np.flatnonzero(np.r_[True, g[1:] != g[:-1]]); en = np.r_[st[1:], len(ms)]
    return dict(t=g[st] * tf, h=np.maximum.reduceat(bid, st), l=np.minimum.reduceat(bid, st), c=bid[en - 1])


def build_trades(s):
    z = np.load(f"{TICKS}/{s['sym']}.npz"); ms, bid, ask = z['ms'], z['bid'], z['ask']
    b = bars(ms, bid, s['tf']); a = wilder_atr(b['h'], b['l'], b['c'], P['atr_n'])
    sig = signals(b['c'], a, P['lookback'], P['min_move'], P['hold'], P['cooldown'])
    idx = np.flatnonzero(sig)
    t_in = (b['t'][idx] + s['tf']) * 60000 + ENTRY_DELAY_MS
    i_in = np.searchsorted(ms, t_in)
    T = []
    for j, k in enumerate(idx):
        i0 = i_in[j]; i1 = i_in[j + 1] if j + 1 < len(idx) else len(ms)
        if i0 >= len(ms) - 1 or i1 - i0 < 2:
            continue
        T.append(dict(sym=s['sym'], tag=s['tag'], side=int(sig[k]), atr=float(a[k]), ms0=int(ms[i0]),
                      ms=ms[i0:i1], bid=bid[i0:i1], ask=ask[i0:i1]))
    return T


def leg_path(t, side):
    """Entry price and exit-side price path for a position on `side` opened at the trade's first tick."""
    if side > 0:
        return t['ask'][0], t['bid'][1:], t['ask'][1:]
    return t['bid'][0], t['ask'][1:], t['bid'][1:]


def first(mask, start=0):
    if start >= len(mask):
        return BIG
    m = mask[start:]
    return start + int(np.argmax(m)) if m.any() else BIG


def run_exit(fav, S, atr, be=None, tp=None, trail=None, trail_act=None, start=0, floor=None):
    """fav: favourable move (exit-side price minus entry, signed). Stop at -S (or at `floor` if given). be/tp/trail/trail_act
    in ATR. Returns exit index (or len-1 = window end) and the reason."""
    n = len(fav)
    stop_lvl = -S if floor is None else floor
    i_sl = first(fav <= stop_lvl, start)
    i_tp = first(fav >= tp * atr, start) if tp else BIG
    i_be = BIG
    if be is not None:
        j = first(fav >= be * atr, start)
        if j < BIG:
            i_be = first(fav <= 0, j + 1)
    i_tr = BIG
    if trail:
        act = (trail_act if trail_act is not None else trail) * atr
        j = first(fav >= act, start)
        if j < BIG and j + 1 < n:
            rm = np.maximum.accumulate(fav[j:])
            lvl = np.maximum(rm - trail * atr, stop_lvl)
            h = fav[j + 1:] <= lvl[:-1]
            i_tr = j + 1 + int(np.argmax(h)) if h.any() else BIG
    i = min(i_sl, i_tp, i_be, i_tr)
    if i >= n:
        return n - 1, 'end'
    return i, ('stop' if i == i_sl else 'tp' if i == i_tp else 'be' if i == i_be else 'trail')


def trade_R(t, side, stop_atr, be, tp, trail, trail_act):
    e, px, _ = leg_path(t, side)
    if len(px) == 0:
        return 0.0, 0.0, 0.0
    fav = side * (px - e); S = stop_atr * t['atr']
    i, why = run_exit(fav, S, t['atr'], be, tp, trail, trail_act)
    return fav[i] / S, fav[:i + 1].max() / S, why


def stats(r):
    r = np.asarray(r)
    if len(r) < 2:
        return dict(n=len(r), mean=0, t=0, win=0, pf=0)
    w = r[r > 0].sum(); l = -r[r < 0].sum()
    return dict(n=len(r), mean=r.mean(), t=r.mean() / r.std(ddof=1) * len(r) ** .5, win=(r > 0.05).mean(), pf=w / l if l else np.inf)


def fmt(d):
    return f"n {d['n']:4d}  mean {d['mean']:+.3f}R  t {d['t']:+.2f}  win {100 * d['win']:.0f}%  PF {d['pf']:.2f}"


# ---------------- part 2: sizing ----------------
def scale_in_pct(t, probe, add, a_atr, post, stop_atr=5.0, tp_r=0.0):
    """Equity % result of one trade: probe `probe`% at entry (stop stop_atr ATR); when the exit-side price is a_atr ATR in
    favour, the probe's stop moves to entry and a second leg is opened at that tick sized so that a return to the probe's
    entry loses `add`%. After the add, the whole position exits at: probe entry (break-even), the trailing stop
    (post = ('trail', k) -> max(entry, peak - k ATR)), the TP (tp_r x the probe R from the probe entry), or the next signal."""
    side = t['side']; e, px, ent2 = leg_path(t, side)
    if len(px) == 0:
        return 0.0, False
    atr = t['atr']; S = stop_atr * atr; fav = side * (px - e)
    j = first(fav >= a_atr * atr)
    i_sl = first(fav <= -S)
    i_tp0 = first(fav >= tp_r * S) if tp_r else BIG
    if j == BIG or i_sl < j or i_tp0 <= j:                           # never confirmed: probe-only trade
        i = min(i_sl, i_tp0)
        i = len(fav) - 1 if i >= len(fav) else i
        return probe * fav[i] / S, False
    e2 = ent2[j]; S2 = side * (e2 - e)                              # add's stop = probe entry
    if S2 <= 0:
        return probe * fav[-1] / S, False
    trail = post[1] if post[0] == 'trail' else None
    i, _ = run_exit(fav, S, atr, None, tp_r * S / atr if tp_r else None, trail, trail_act=0.0 if trail else None, start=j + 1, floor=0.0)
    x = fav[i]
    return probe * x / S + add * (side * (px[i] - e2)) / S2, True


def equity_path(pcts):
    eq = np.cumprod(1 + np.asarray(pcts) / 100.0)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    dd = (1 - eq / peak).max() if len(eq) else 0
    return eq[-1] if len(eq) else 1.0, dd


def streak_ladder(R, ladder, reset_on=-0.3):
    """Risk steps up the ladder after each win (R > 0.05), back to the bottom after a loss worse than reset_on R."""
    out = []; k = 0
    for r in R:
        out.append(ladder[k] * r)
        if r > 0.05:
            k = min(k + 1, len(ladder) - 1)
        elif r < reset_on:
            k = 0
    return out


def main():
    T = []
    for s in STRATS:
        tt = build_trades(s); T += tt
        print(f"{s['tag']}: {len(tt)} replayed trades, {time.strftime('%Y-%m-%d', time.gmtime(tt[0]['ms0'] / 1000))} .. "
              f"{time.strftime('%Y-%m-%d', time.gmtime(tt[-1]['ms0'] / 1000))}")
    T.sort(key=lambda t: t['ms0'])
    tr = [t for t in T if t['ms0'] < SPLIT_MS]; te = [t for t in T if t['ms0'] >= SPLIT_MS]
    print(f'train {len(tr)} trades, test {len(te)} trades (split 2026-10-15)')

    # ---------- part 1 ----------
    variants = {'LIVE (stop 5, BE 4, rev; Boom TP 2R)': None}
    for st in (3, 4, 5, 6):
        for be in (None, 2, 3, 4):
            for tp in (None, 5, 10, 15):
                variants[f'stop {st} BE {be} TP {tp}'] = dict(stop_atr=st, be=be, tp=tp, trail=None, trail_act=None)
            for trl in (2, 3, 5):
                variants[f'stop {st} BE {be} trail {trl}'] = dict(stop_atr=st, be=be, tp=None, trail=trl, trail_act=trl)
    res = {}
    for name, v in variants.items():
        for tagset in ('Jump50', 'Boom1000'):
            for part, TT in (('train', tr), ('test', te)):
                rr = []; rf = []
                for t in TT:
                    if t['sym'] != tagset:
                        continue
                    if v is None:
                        tp = 2 * 5.0 if t['sym'] == 'Boom1000' else None
                        a = dict(stop_atr=5.0, be=4.0, tp=tp, trail=None, trail_act=None)
                    else:
                        a = v
                    rr.append(trade_R(t, t['side'], **a)[0]); rf.append(trade_R(t, -t['side'], **a)[0])
                res[(name, tagset, part)] = (np.array(rr), np.array(rf))
    for sym in ('Jump50', 'Boom1000'):
        print(f'\n=== Part 1 exits: {sym} (R = stop distance; "flip" = same exits on the opposite side)')
        live = res[(list(variants)[0], sym, 'train')]
        print(f"  LIVE   train {fmt(stats(live[0]))} | flip {fmt(stats(live[1]))}")
        live = res[(list(variants)[0], sym, 'test')]
        print(f"  LIVE   test  {fmt(stats(live[0]))} | flip {fmt(stats(live[1]))}")
        ranked = sorted([n for n in variants if n != list(variants)[0]], key=lambda n: -stats(res[(n, sym, 'train')][0])['mean'])
        print('  top 10 on train, then their test:')
        for n in ranked[:10]:
            a, b = res[(n, sym, 'train')][0], res[(n, sym, 'test')][0]
            print(f"   {n:28s} train {fmt(stats(a))}\n   {'':28s} test  {fmt(stats(b))}  flip-test {stats(res[(n, sym, 'test')][1])['mean']:+.3f}")
        allm = np.array([stats(res[(n, sym, 'test')][0])['mean'] for n in variants])
        print(f"  all {len(variants)} variants on test: mean R median {np.median(allm):+.3f}, best {allm.max():+.3f}, worst {allm.min():+.3f}, "
              f"share > 0: {(allm > 0).mean():.0%}")

    # MFE profile of live exits
    for sym in ('Jump50', 'Boom1000'):
        mf = np.array([trade_R(t, t['side'], 5.0, None, None, None, None)[1] for t in T if t['sym'] == sym])
        print(f'\n{sym} MFE before stop/next signal (no BE/TP), R=5 ATR: '
              + ', '.join(f'>= {x}R {np.mean(mf >= x):.0%}' for x in (0.2, 0.4, 0.6, 0.8, 1, 1.5, 2, 3)))

    # ---------- part 2 ----------
    def live_R(t):
        tp = 10.0 if t['sym'] == 'Boom1000' else None
        return trade_R(t, t['side'], 5.0, 4.0, tp, None, None)[0]
    for part, TT in (('ALL', T), ('train', tr), ('test', te)):
        R = np.array([live_R(t) for t in TT])
        print(f'\n=== Part 2 sizing, LIVE exits, {part} ({len(TT)} trades, both strategies in entry order, compounding)')
        rows = []
        for k in (1, 2, 3, 5):
            rows.append((f'fixed {k}%', *equity_path(k * R)))
        for lad in ([1, 2, 3, 4, 5], [1, 3, 5], [2, 5], [1, 5]):
            rows.append((f'ladder {lad} reset on loss', *equity_path(streak_ladder(R, lad))))
        for probe in (1.0, 1.5, 2.5):
            for a_atr in (1.0, 2.0, 3.0, 4.0):
                for post in (('be', None), ('trail', 3), ('trail', 5)):
                    p = []; adds = 0
                    for t in TT:
                        tp = 2.0 if t['sym'] == 'Boom1000' else 0.0
                        x, ad = scale_in_pct(t, probe, 5.0, a_atr, post, tp_r=tp); p.append(x); adds += ad
                    lab = f"scale-in probe {probe}% add 5% @ +{a_atr:g} ATR, then {post[0]}{'' if post[1] is None else ' ' + str(post[1])}"
                    rows.append((lab + f' [adds {adds}/{len(TT)}]', *equity_path(p)))
        for lab, fin, dd in rows:
            print(f'  {lab:70s} x{fin:7.3f}  maxDD {100 * dd:5.1f}%  ret/DD {((fin - 1) / dd if dd else 0):+6.2f}')
    # losing streak exposure at 5% vs ladder
    R = np.array([live_R(t) for t in T]); s = 0; mx = 0
    for r in R:
        s = s + 1 if r < -0.3 else 0; mx = max(mx, s)
    print(f'\nlongest streak of full-ish losses (< -0.3R) in replay: {mx}  -> at 5% risk ~{100 * (1 - 0.95 ** mx):.0f}% drawdown')


if __name__ == '__main__':
    main()
