"""
Trend-aligned H&S on a lower timeframe (default 5M), with an HTF trend filter (1H and 4H).
  HTF up   -> buy INVERSE H&S   (long)
  HTF down -> sell H&S TOP      (short)
Pattern rules = hs.detect (pivots k=3/5, geometry, neckline break) applied to LTF bars.
HTF trend: last CLOSED HTF bar, close vs EMA(50) and EMA(50) vs 5 bars earlier.
Stop: right shoulder +/- 0.2 ATR(LTF). Skip if risk < 0.5 or > 4 ATR(LTF), or risk < 3 x round-trip cost.
Targets 1R / 2R / measured move. Holds: intraday (8h, flat 16:45 EST) and swing (48h, flat Friday).
Placebo: random LTF bars in same HTF state & side, same stop in ATR units, same R target, 100 draws.
Placebo is compared GROSS (no cost) vs the pattern GROSS, so time-of-day differences in cost/R do not bias it.
"""
import sys, numpy as np, pandas as pd
from lib import load_m1, resample, atr, ema, COST
from hs import detect, sim, CACHE, NO_ENTRY

LTF = sys.argv[1]            # e.g. 5min
SYMS = sys.argv[2:]
HTFS = {'1H': '1h', '4H': '4h'}
rng = np.random.default_rng(11)
rows, trades = [], []
for sym in SYMS:
    m1 = load_m1(f'{CACHE}/{sym}_m1.npz'); lt = resample(m1, LTF); a = atr(lt); A = a.values
    step = pd.Timedelta(LTF)
    idx = m1.index; t1 = idx.values
    tod = (((idx.hour * 60 + idx.minute).values + 420) % 1440).astype(np.int64)
    tday = (idx + pd.Timedelta(hours=7)).normalize()
    day = tday.values.astype('datetime64[D]').astype(np.int64); dow = tday.dayofweek.values.astype(np.int64)
    o, hh, ll, cc = (m1[x].values.astype(np.float64) for x in 'ohlc')
    close_t = (lt.index + step).values
    J = np.minimum(np.searchsorted(t1, close_t), len(t1) - 1)
    et = pd.DatetimeIndex(t1[J]); etod = et.hour * 60 + et.minute
    okbar = ~((etod >= NO_ENTRY[0]) & (etod < NO_ENTRY[1])) & ((t1[J] - close_t) <= np.timedelta64(30, 'm')) & (A > 0)
    trend = {}
    for name, rule in HTFS.items():
        H = resample(m1, rule); e = ema(H.c, 50)
        up = (H.c > e) & (e > e.shift(5)); dn = (H.c < e) & (e < e.shift(5))
        key = (lt.index + step).floor(rule) - pd.Timedelta(rule)
        trend[name] = (up.astype(int) - dn.astype(int)).reindex(key).fillna(0).values
    for k in (3, 5):
        P = detect(lt, a, k)
        print(sym, LTF, f'k={k}', 'patterns', len(P), flush=True)
        bars_all = P.bar.values; side_all = P.side.values.astype(np.int64)
        j_all = J[bars_all].astype(np.int64); e_all = o[j_all]
        sl_all = np.where(side_all == -1, P.H3.values + 0.2 * P.atr.values, P.H3.values - 0.2 * P.atr.values)
        risk_all = side_all * (e_all - sl_all)
        base_ok = okbar[bars_all] & (risk_all >= 0.5 * P.atr.values) & (risk_all <= 4 * P.atr.values) & (risk_all >= 3 * COST[sym])
        meas_all = P.neck.values + side_all * P.h.values
        for hname, tr in trend.items():
            t_at = tr[bars_all]
            m = base_ok & (side_all == t_at)
            bars, side, j, e, risk, meas = bars_all[m], side_all[m], j_all[m], e_all[m], risk_all[m], meas_all[m]
            ratm = risk / A[bars]; t4 = t_at[m]
            pool = {(s4, sd): np.where(okbar & (tr == s4))[0] for s4 in (1, -1) for sd in (1, -1)}
            for mode, mh, sw in (('intraday', 480, False), ('swing48h', 2880, True)):
                for tgt in ('1R', '2R', 'measured'):
                    tp = e + side * risk if tgt == '1R' else e + side * 2 * risk if tgt == '2R' else meas
                    R, hold = sim(o, hh, ll, cc, tod, day, dow, j, side, e - side * risk, tp, COST[sym], mh, sw)
                    R0, _ = sim(o, hh, ll, cc, tod, day, dow, j, side, e - side * risk, tp, 0.0, mh, sw)
                    # placebo (only for R-multiple targets; measured has no ATR analogue -> use 2R-equivalent of its own R)
                    rmult = np.where(np.isfinite(R), (side * (tp - e)) / risk, 2.0)
                    pl = []
                    for rep in range(100):
                        pb = np.empty(len(side), np.int64)
                        for key_ in set(zip(t4.tolist(), side.tolist())):
                            mk = (t4 == key_[0]) & (side == key_[1]); pb[mk] = rng.choice(pool[key_], mk.sum())
                        jj = J[pb].astype(np.int64); ee = o[jj]; rk = ratm * A[pb]
                        Rp, _ = sim(o, hh, ll, cc, tod, day, dow, jj, side, ee - side * rk, ee + side * rmult * rk, 0.0, mh, sw)  # GROSS placebo
                        pl.append([np.nanmean(Rp[side == sd]) for sd in (1, -1)])
                    pl = np.array(pl)
                    for c_, sd in enumerate((1, -1)):
                        f = np.isfinite(R) & (side == sd); r_ = R[f]
                        if len(r_) < 10: continue
                        yrs = pd.Series(r_, index=lt.index[bars[f]].year).groupby(level=0).sum()
                        rows.append(dict(sym=sym, htf=hname, pattern='inverse/long' if sd == 1 else 'top/short', k=k, mode=mode, target=tgt,
                                         n=len(r_), win=(r_ > 0).mean(), meanR=r_.mean(), gross=np.nanmean(R0[f]),
                                         t=r_.mean() / r_.std(ddof=1) * np.sqrt(len(r_)), yrs_pos=f'{(yrs > 0).sum()}/{len(yrs)}',
                                         placebo_gross=pl[:, c_].mean(), beats_placebo=(pl[:, c_] < np.nanmean(R0[f])).mean()))
                    f = np.isfinite(R)
                    trades.append(pd.DataFrame(dict(sym=sym, htf=hname, ltf=LTF, k=k, mode=mode, target=tgt, t=lt.index[bars[f]], side=side[f], R=R[f],
                                                    entry=pd.DatetimeIndex(t1[j[f]]), hold_min=hold[f], ratm=ratm[f])))
D = pd.DataFrame(rows); tag = LTF.replace('min', 'm')
D.to_csv(f'out/hs{tag}_summary.csv', index=False); pd.concat(trades).to_csv(f'out/hs{tag}_trades.csv', index=False)
print('saved', len(D), 'cells')
