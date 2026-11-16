"""
Head & Shoulders on 15M, aligned with the 1H trend.  XAUUSD / USDJPY / AUDUSD, 2020 ->.

Pattern (top = short; inverse = long, mirrored). All pivots are fractal pivots of width k,
known only k bars after they print, and kept in an alternating high/low list built online.
  LS=H1, L1, Head=H2, L2, RS=H3  (last five pivots, ending in a high)
  neckline   : line through L1 and L2
  height h   : H2 - neckline(at head)            >= 1.5 x ATR15
  head       : H2 - max(H1, H3)                  >= 0.15 h
  shoulders  : |H1 - H3| <= 0.40 h; each shoulder >= 0.30 h beyond the neckline
  neckline   : |L1 - L2|                         <= 0.50 h
  width      : 8 .. 120 bars from LS to RS; time symmetry (H2-H1)/(H3-H2) in [0.4, 2.5]
  trigger    : first 15M close below the neckline, from RS confirmation up to 24 bars later,
               not more than 0.75 ATR below it (no chasing); void if price trades above H3 first
Entry: open of the next minute. Stop: H3 + 0.2 ATR. Skip if risk < 0.5 or > 4 ATR.
"""
import sys, os, numpy as np, pandas as pd
from numba import njit
from lib import load_m1, resample, atr, ema, COST

import os
CACHE = os.environ.get('WF_DATA', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data'))
EOD = (16 * 60 + 45 + 420) % 1440  # minutes since 17:00 EST
NO_ENTRY = (16 * 60, 19 * 60)


def detect(m15, a15, k):
    h, l, c = m15.h.values, m15.l.values, m15.c.values
    A = a15.values; n = len(c)
    piv = []   # (type +1 high / -1 low, bar index, price)
    out = []
    armed = []  # active patterns waiting for neckline break
    for t in range(2 * k, n):
        i = t - k  # candidate pivot bar, confirmed at t
        new = None
        if h[i] == h[i - k:t + 1].max(): new = (1, i, h[i])
        if l[i] == l[i - k:t + 1].min(): new = (-1, i, l[i]) if new is None else None  # outside bar: ambiguous, skip
        if new is not None:
            if piv and piv[-1][0] == new[0]:
                if (new[0] == 1 and new[2] > piv[-1][2]) or (new[0] == -1 and new[2] < piv[-1][2]): piv[-1] = new
                new_added = piv[-1] is new
            else:
                piv.append(new); new_added = True
            if new_added and len(piv) >= 5:
                p = piv[-5:]
                s = p[4][0]   # +1 -> top (short), -1 -> inverse (long)
                if [q[0] for q in p] == [s, -s, s, -s, s]:
                    (_, i1, H1), (_, j1, L1), (_, i2, H2), (_, j2, L2), (_, i3, H3) = p
                    slope = (L2 - L1) / (j2 - j1)
                    neck = lambda x: L1 + slope * (x - j1)
                    hgt = s * (H2 - neck(i2)); a = A[t]
                    ok = (a > 0 and hgt >= 1.5 * a and s * (H2 - (max(H1, H3) if s == 1 else min(H1, H3))) >= 0.15 * hgt
                          and abs(H1 - H3) <= 0.40 * hgt and s * (H1 - neck(i1)) >= 0.3 * hgt and s * (H3 - neck(i3)) >= 0.3 * hgt and abs(L1 - L2) <= 0.50 * hgt
                          and 8 <= i3 - i1 <= 120 and 0.4 <= (i2 - i1) / max(i3 - i2, 1) <= 2.5)
                    if ok:
                        armed.append(dict(s=s, H3=H3, L1=L1, j1=j1, slope=slope, h=hgt, conf=t, i1=i1, i3=i3))
        keep = []
        for p in armed:
            s = p['s']
            if s * (h[t] if s == 1 else l[t]) > s * p['H3'] or t - p['conf'] > 24:
                continue  # invalidated or expired
            nk = p['L1'] + p['slope'] * (t - p['j1'])
            if s * (c[t] - nk) < 0:
                if s * (nk - c[t]) <= 0.75 * A[t]:
                    out.append(dict(bar=t, side=-s, neck=nk, H3=p['H3'], h=p['h'], atr=A[t], width=p['i3'] - p['i1']))
                continue  # broke (traded or chased): done either way
            keep.append(p)
        armed = keep
    return pd.DataFrame(out)


@njit(cache=True)
def sim(o, h, l, c, tod, day, dow, ent, side, sl, tp, cost, max_hold, swing):
    n = ent.shape[0]; R = np.full(n, np.nan); hold = np.zeros(n, np.int64)
    for i in range(n):
        j = ent[i]
        if j < 0: continue
        e = o[j]; risk = side[i] * (e - sl[i])
        if risk <= 0 or side[i] * (tp[i] - e) <= 0: continue
        d0 = day[j]; k = j; px = e
        while True:
            end = k >= o.shape[0] or k - j >= max_hold
            if not end:
                if swing: end = (dow[k] == 4 and tod[k] >= 1425) or (k > j and day[k] != day[k - 1] and dow[k - 1] == 4)
                else: end = day[k] != d0 or tod[k] >= 1425
            if end:
                px = c[k - 1] if k > j else e; break
            if side[i] == 1:
                if o[k] <= sl[i]: px = o[k]; break
                if l[k] <= sl[i]: px = sl[i]; break
                if h[k] >= tp[i]: px = tp[i]; break
            else:
                if o[k] >= sl[i]: px = o[k]; break
                if h[k] >= sl[i]: px = sl[i]; break
                if l[k] <= tp[i]: px = tp[i]; break
            k += 1
        R[i] = (side[i] * (px - e) - cost) / risk; hold[i] = k - j
    return R, hold


def run(sym):
    m1 = load_m1(f'{CACHE}/{sym}_m1.npz')
    m15, h1 = resample(m1, '15min'), resample(m1, '1h')
    a15 = atr(m15)
    e50 = ema(h1.c, 50); up = (h1.c > e50) & (e50 > e50.shift(5)); dn = (h1.c < e50) & (e50 < e50.shift(5))
    key = (m15.index + pd.Timedelta(minutes=15)).floor('1h') - pd.Timedelta(hours=1)
    trend = (up.astype(int) - dn.astype(int)).reindex(key).fillna(0).values   # +1 up, -1 down, 0 neither
    idx = m1.index
    tod = (((idx.hour * 60 + idx.minute).values + 420) % 1440).astype(np.int64)
    tday = (idx + pd.Timedelta(hours=7)).normalize()
    day = tday.values.astype('datetime64[D]').astype(np.int64); dow = tday.dayofweek.values.astype(np.int64)
    o, hh, ll, cc = (m1[x].values.astype(np.float64) for x in 'ohlc')
    t1 = idx.values
    rows = []
    for k in (3, 5):
        P = detect(m15, a15, k)
        if P.empty: continue
        close_t = (m15.index[P.bar.values] + pd.Timedelta(minutes=15)).values
        j = np.searchsorted(t1, close_t); j = np.minimum(j, len(t1) - 1)
        et = pd.DatetimeIndex(t1[j]); etod = et.hour * 60 + et.minute
        okent = ~((etod >= NO_ENTRY[0]) & (etod < NO_ENTRY[1])) & ((t1[j] - close_t) <= np.timedelta64(30, 'm'))
        P['ent'] = np.where(okent, j, -1)
        P['trend'] = trend[P.bar.values]
        P['t'] = m15.index[P.bar.values]
        side = P.side.values.astype(np.int64)
        e = np.where(P.ent >= 0, o[np.maximum(P.ent.values, 0)], np.nan)
        sl = np.where(side == -1, P.H3.values + 0.2 * P.atr.values, P.H3.values - 0.2 * P.atr.values)
        risk = side * (e - sl)
        okr = (risk >= 0.5 * P.atr.values) & (risk <= 4 * P.atr.values)
        entv = np.where(okr, P.ent.values, -1).astype(np.int64)
        exits = {'measured': P.neck.values + side * P.h.values,
                 '1R': e + side * risk, '2R': e + side * 2 * risk,
                 'atr2': e + side * 2 * P.atr.values}
        for ename, tp in exits.items():
            sl_e = np.where(ename == 'atr2', e - side * 2 * P.atr.values, sl)
            for mode, mh, sw in (('intraday', 480, False), ('swing48h', 48 * 60, True)):
                for cname, cst in (('cost', COST[sym]), ('nocost', 0.0)):
                    R, hold = sim(o, hh, ll, cc, tod, day, dow, entv, side, np.nan_to_num(sl_e), np.nan_to_num(tp), cst, mh, sw)
                    d = P[['t', 'side', 'trend', 'width']].copy(); d['R'] = R; d['hold'] = hold
                    d['k'], d['exit'], d['mode'], d['costs'], d['sym'] = k, ename, mode, cname, sym
                    rows.append(d[np.isfinite(R)])
        print(sym, f'k={k}', 'patterns', len(P), 'tradable', int((entv >= 0).sum()),
              'tops', int((P.side == -1).sum()), 'inverse', int((P.side == 1).sum()), flush=True)
    return pd.concat(rows)


if __name__ == '__main__':
    allr = pd.concat([run(s) for s in sys.argv[1:]])
    allr.to_pickle('out/hs_trades.pkl')
