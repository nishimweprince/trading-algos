"""DSI-strategy portability backtest on Deriv MT5 M1 (69.5 days), spikes included. Read-only, offline.

Signals: deriv-jev rules.generic_rules (trend + fade) plus 'dz' = drift z-score on the jump-free path
(the research's portable core). Entry next M1 open after the tf bar closes. R = max(stop_atr*ATR,
1.2*broker min stop + spread). One spread per round trip (DSI: the 24h mean spread during |15m z|>2,
i.e. trend-conditional). Swap pro rata. Stops on spike bars (range > 6 x M1 ATR) fill at the bar extreme.
IS = first half, OOS = second half."""
import json, sys, itertools
import numpy as np
from numba import njit
sys.path.insert(0, '/opt/trading-algos/services/deriv-jev')
from rules import generic_rules, cross_up, cross_dn, lag
from engine import resample, atr

SV = json.load(open('/opt/dsi-analogs/survey.json'))
CODES = sys.argv[1].split(',') if len(sys.argv) > 1 else ['CBF1000', 'CBF500', 'CBF300', 'CBF150', 'RB100', 'RB200', 'TREK_U', 'TREK_D', 'VSW_L', 'DSI10', 'DSI20', 'DSI30', 'V75']
EXITS = {'trail': (2.0, 0.0, 1.5, 480), 'tgt1': (2.0, 1.0, 0.0, 240), 'tgt2': (1.0, 2.0, 0.0, 480), 'tgt05': (2.0, 0.5, 0.0, 240)}


@njit(cache=True)
def sim(o, h, l, c, a1, ent, side, R, tpR, trR, mh, cost, swm, swabs, jk):
    n = len(o); out_r = np.full(len(ent), np.nan); busy = -1
    for j in range(len(ent)):
        e = ent[j]
        if e <= busy or e >= n:
            continue
        s = side[j]; r0 = R[j]; px = o[e]; sl = px - s * r0; best = px
        tp = px + s * tpR * r0 if tpR > 0 else np.nan
        res = np.nan; i = e
        while i < n:
            held = i - e + 1; jump = (h[i] - l[i]) > jk * a1[i]
            if s > 0:
                if l[i] <= sl:
                    res = (l[i] if jump else min(sl, o[i])) - px; break
                if tpR > 0 and h[i] >= tp:
                    res = tp - px; break
                best = max(best, h[i])
                if trR > 0 and best - px >= r0:
                    sl = max(sl, best - trR * r0)
            else:
                if h[i] >= sl:
                    res = px - (h[i] if jump else max(sl, o[i])); break
                if tpR > 0 and l[i] <= tp:
                    res = px - tp; break
                best = min(best, l[i])
                if trR > 0 and px - best >= r0:
                    sl = min(sl, best + trR * r0)
            if held >= mh + 1:
                res = s * (c[i] - px); break
            i += 1
        if np.isnan(res):
            break
        busy = i
        out_r[j] = (res - cost - (swm * px + swabs) * (i - e + 1)) / r0
    return out_r


def dz_rules(b, L_tf, a):
    R = {}
    sd = np.nanstd(np.diff(L_tf)) + 1e-15
    for n in (4, 16, 48):
        z = (L_tf - lag(L_tf, n)) / (sd * np.sqrt(n))
        for q in (1.0, 1.5, 2.0):
            s = np.zeros(len(z), np.int8); s[cross_up(z, q)] = 1; s[cross_dn(z, -q)] = -1
            R[f'dz{n}_q{q}_trend'] = s; R[f'dz{n}_q{q}_fade'] = -s
    return R


def stats(r, et, lo, hi):
    m = (et >= lo) & (et < hi) & np.isfinite(r); x = r[m]
    if len(x) < 2:
        return dict(n=len(x), meanR=0.0, t=0.0, hit=0.0, sumR=0.0)
    d = (et[m] // 1440); u, inv = np.unique(d, return_inverse=True); dr = np.bincount(inv, x)
    t = dr.mean() / (dr.std(ddof=1) / np.sqrt(len(dr))) if len(dr) > 2 and dr.std() > 0 else 0.0
    return dict(n=int(len(x)), meanR=round(float(x.mean()), 3), t=round(float(t), 2), hit=round(float((x > 0).mean()), 3), sumR=round(float(x.sum()), 1))


rows = []
for code in CODES:
    S = SV[code]; d = np.load(f'/opt/dsi-analogs/m1/{code}.npz')
    t, o, h, l, c = d['t'], d['o'].astype(float), d['h'].astype(float), d['l'].astype(float), d['c'].astype(float)
    spread = S['spread_trend2'] if code.startswith('DSI') else max(S['spread_mean'], S['point'])
    swm = -S['swap_long'] / 100 / 525600 if S['swap_mode'] == 5 else 0.0
    swabs = -S['swap_long'] * S['point'] / 1440 if S['swap_mode'] == 1 else 0.0
    min_r = 1.2 * S['min_stop'] + spread
    a1 = atr(dict(h=h, l=l, c=c))
    r1 = np.diff(np.log(c), prepend=np.log(c[0])); mad = np.median(np.abs(r1)) * 1.4826 + 1e-15
    Lm = np.cumsum(np.where(np.abs(r1) > 8 * mad, 0.0, r1))
    mid = t[0] + (t[-1] - t[0]) // 2
    for tf in (1, 5, 15):
        b = resample(t, o, h, l, c, tf, int(t[-1]) + 1); a = atr(b)
        ends = np.searchsorted(t, b['t'] + tf) - 1; L_tf = Lm[np.clip(ends, 0, len(Lm) - 1)]
        rules = generic_rules(b, a); rules.update(dz_rules(b, L_tf, a))
        ent_all = np.searchsorted(t, b['t'] + tf)
        for rn, sig in rules.items():
            k = np.flatnonzero((sig != 0) & (np.arange(len(sig)) >= 300) & np.isfinite(a) & (a > 0))
            if len(k) == 0:
                continue
            ent = ent_all[k].astype(np.int64); side = sig[k].astype(np.int64)
            for ex, (sa, tpR, trR, mh) in EXITS.items():
                R = np.maximum(sa * a[k], min_r)
                r = sim(o, h, l, c, a1, ent, side, R, tpR, trR, mh, spread, swm, swabs, 6.0)
                et = t[np.clip(ent, 0, len(t) - 1)]
                widened = float((sa * a[k] < min_r).mean())
                rows.append(dict(code=code, tf=tf, rule=rn, exit=ex, widened=round(widened, 2),
                                 IS=stats(r, et, t[0], mid), OOS=stats(r, et, mid, t[-1] + 1)))
    print(code, 'done', flush=True)
json.dump(rows, open(f'/opt/dsi-analogs/bt_{"_".join(CODES) if len(CODES) < 4 else "all"}.json', 'w'))
