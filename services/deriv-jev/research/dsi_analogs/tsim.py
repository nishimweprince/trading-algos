"""Tick-level replay of bar-backtest winners. Signals from M1 bars (same as bt.py); execution on real ticks:
buy at ask / sell at bid, entry at first tick >= signal time + latency, stop/target/trail checked on every tick
(long exits on bid, short exits on ask), fill = first tick >= trigger time + latency. Variants chosen by IS t only."""
import json, sys
import numpy as np
from numba import njit
sys.path.insert(0, '/opt/trading-algos/services/deriv-jev'); sys.path.insert(0, '/opt/dsi-analogs')
from rules import generic_rules
from engine import resample, atr
import importlib.util
spec = importlib.util.spec_from_file_location('bt_rules', '/opt/dsi-analogs/dzr.py'); dzr = importlib.util.module_from_spec(spec); spec.loader.exec_module(dzr)

SV = json.load(open('/opt/dsi-analogs/survey.json'))
ROWS = json.load(open('/opt/dsi-analogs/bt_all.json'))
EXITS = {'trail': (2.0, 0.0, 1.5, 480), 'tgt1': (2.0, 1.0, 0.0, 240), 'tgt2': (1.0, 2.0, 0.0, 480), 'tgt05': (2.0, 0.5, 0.0, 240)}


@njit(cache=True)
def tsim(ms, bid, ask, ent_ms, side, R, tpR, trR, mh_ms, lat, swm, swabs):
    n = len(ms); out = np.full(len(ent_ms), np.nan); hold = np.zeros(len(ent_ms)); busy = -1
    k = 0
    for j in range(len(ent_ms)):
        t0 = ent_ms[j] + lat
        while k < n and ms[k] < t0:
            k += 1
        if k >= n:
            break
        if ms[k] <= busy:
            continue
        s = side[j]; r0 = R[j]
        px = ask[k] if s > 0 else bid[k]
        sl = px - s * r0; tp = px + s * tpR * r0; best = px; e_ms = ms[k]
        i = k + 1; trig = -1
        while i < n:
            q = bid[i] if s > 0 else ask[i]
            if s > 0:
                if q <= sl or (tpR > 0 and q >= tp):
                    trig = i; break
                if q > best:
                    best = q
                    if trR > 0 and best - px >= r0:
                        sl = max(sl, best - trR * r0)
            else:
                if q >= sl or (tpR > 0 and q <= tp):
                    trig = i; break
                if q < best:
                    best = q
                    if trR > 0 and px - best >= r0:
                        sl = min(sl, best + trR * r0)
            if ms[i] - e_ms >= mh_ms:
                trig = i; break
            i += 1
        if trig < 0:
            break
        f = trig
        while f < n - 1 and ms[f] < ms[trig] + lat:
            f += 1
        xq = bid[f] if s > 0 else ask[f]
        mins = (ms[f] - e_ms) / 60000.0
        out[j] = (s * (xq - px) - (swm * px + swabs) * mins) / r0
        hold[j] = mins; busy = ms[f]
    return out, hold


def daily_t(r, day):
    u, inv = np.unique(day, return_inverse=True); dr = np.bincount(inv, r)
    return dr.mean() / (dr.std(ddof=1) / np.sqrt(len(dr))) if len(dr) > 2 else 0.0


codes = sys.argv[1].split(',')
for code in codes:
    S = SV[code]; d = np.load(f'/opt/dsi-analogs/m1/{code}.npz'); T = np.load(f'/opt/dsi-analogs/ticks/{code}.npz')
    t, o, h, l, c = d['t'], d['o'].astype(float), d['h'].astype(float), d['l'].astype(float), d['c'].astype(float)
    ms, bid, ask = T['ms'], T['bid'], T['ask']; ok = (bid > 0) & (ask > 0); ms, bid, ask = ms[ok], bid[ok], ask[ok]
    spread = S['spread_trend2'] if code.startswith('DSI') else max(S['spread_mean'], S['point'])
    swm = -S['swap_long'] / 100 / 525600 if S['swap_mode'] == 5 else 0.0
    swabs = -S['swap_long'] * S['point'] / 1440 if S['swap_mode'] == 1 else 0.0
    min_r = 1.2 * S['min_stop'] + spread
    r1 = np.diff(np.log(c), prepend=np.log(c[0])); mad = np.median(np.abs(r1)) * 1.4826 + 1e-15
    Lm = np.cumsum(np.where(np.abs(r1) > 8 * mad, 0.0, r1))
    cand = [r for r in ROWS if r['code'] == code and r['IS']['n'] >= 30 and r['OOS']['n'] >= 30]
    picks = sorted(cand, key=lambda r: -r['IS']['t'])[:4]
    if code == 'DSI10':
        picks += [r for r in ROWS if r['code'] == 'DSI10' and r['exit'] == 'trail' and (r['tf'], r['rule']) in ((1, 'rsi20_trend'), (5, 'mom4_q1.5_trend'))]
    for P in picks:
        tf = P['tf']; sa, tpR, trR, mh = EXITS[P['exit']]
        b = resample(t, o, h, l, c, tf, int(t[-1]) + 1); a = atr(b)
        ends = np.searchsorted(t, b['t'] + tf) - 1; L_tf = Lm[np.clip(ends, 0, len(Lm) - 1)]
        rules = generic_rules(b, a); rules.update(dzr.dz_rules(b, L_tf, a)); sig = rules[P['rule']]
        k = np.flatnonzero((sig != 0) & (np.arange(len(sig)) >= 300) & np.isfinite(a) & (a > 0))
        ent_ms = ((b['t'][k] + tf) * 60000).astype(np.int64); keep = ent_ms >= ms[0]
        k, ent_ms = k[keep], ent_ms[keep]
        R = np.maximum(sa * a[k], min_r)
        line = f"{code:7} M{tf:<2} {P['rule']:17} {P['exit']:5} barOOS {P['OOS']['meanR']:+.3f}/t{P['OOS']['t']:.1f} | tick:"
        for lat in (0, 1000, 2000):
            r, hold = tsim(ms, bid, ask, ent_ms, sig[k].astype(np.int64), R, tpR, trR, mh * 60000 + 60000, lat, swm, swabs)
            m = np.isfinite(r); x = r[m]; day = ent_ms[m] // 86400000
            top = np.sort(x)[::-1]; share5 = top[:max(1, len(x) // 20)].sum() / x.sum() if x.sum() > 0 else np.nan
            line += f"  L{lat//1000}s n{len(x)} {x.mean():+.3f} t{daily_t(x, day):.1f} hit{(x > 0).mean():.2f}"
            if lat == 1000:
                tail = f" top5%share {share5:.2f} hold{np.median(hold[m]):.0f}m Rmed {np.median(R):.2f} px {c[-1]:.0f}"
        print(line + tail, flush=True)
