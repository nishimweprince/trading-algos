"""
Backtest the Autochartist >70% patterns (patterns.detect_all) on one LTF for XAUUSD / USDJPY / AUDUSD.
Filters: none | 1H aligned | 4H aligned (trade side must equal the HTF trend: close vs rising/falling EMA50 on
last CLOSED HTF bar). Exits: pattern target ('measured'), 1R, 2R. Holds: scalp (2h) and intraday (8h), both flat
16:45 EST. Entry next 1-minute open after the signal close; no entries 16:00-18:59 EST.
Risk filter: 0.5..4 ATR(LTF) and >= 3x round-trip cost; measured target must be beyond entry.
Placebo (gross): random LTF bars in the same HTF state (or any bar for 'none'), same side, same stop in ATR
units, same R multiple, 50 draws; compared with the pattern's gross R.
Usage: python3 ac_run.py 5min
"""
import sys, numpy as np, pandas as pd
from lib import load_m1, resample, atr, ema, COST
from hs import sim, CACHE, NO_ENTRY
from patterns import detect_all

LTF = sys.argv[1]; SYMS = ['XAUUSD', 'USDJPY', 'AUDUSD']
rng = np.random.default_rng(21); rows, trades = [], []
for sym in SYMS:
    m1 = load_m1(f'{CACHE}/{sym}_m1.npz'); lt = resample(m1, LTF); a = atr(lt); A = a.values; step = pd.Timedelta(LTF)
    idx = m1.index; t1 = idx.values
    tod = (((idx.hour * 60 + idx.minute).values + 420) % 1440).astype(np.int64)
    tday = (idx + pd.Timedelta(hours=7)).normalize()
    day = tday.values.astype('datetime64[D]').astype(np.int64); dow = tday.dayofweek.values.astype(np.int64)
    o, hh, ll, cc = (m1[x].values.astype(np.float64) for x in 'ohlc')
    close_t = (lt.index + step).values
    J = np.minimum(np.searchsorted(t1, close_t), len(t1) - 1)
    et = pd.DatetimeIndex(t1[J]); etod = et.hour * 60 + et.minute
    okbar = ~((etod >= NO_ENTRY[0]) & (etod < NO_ENTRY[1])) & ((t1[J] - close_t) <= np.timedelta64(30, 'm')) & (A > 0)
    trend = {'none': np.zeros(len(lt), int)}
    for name, rule in (('1H', '1h'), ('4H', '4h')):
        H = resample(m1, rule); e = ema(H.c, 50)
        up = (H.c > e) & (e > e.shift(5)); dn = (H.c < e) & (e < e.shift(5))
        key = (lt.index + step).floor(rule) - pd.Timedelta(rule)
        trend[name] = (up.astype(int) - dn.astype(int)).reindex(key).fillna(0).values
    pools = {f: {(s4, sd): np.where(okbar & ((trend[f] == s4) if f != 'none' else True))[0] for s4 in (1, -1, 0) for sd in (1, -1)} for f in trend}
    for k in (3, 5):
        S = detect_all(lt, a, k)
        print(sym, LTF, f'k={k}', len(S), 'signals', flush=True)
        bars = S.bar.values; side = S.side.values.astype(np.int64)
        j = J[bars].astype(np.int64); e = o[j]; risk = side * (e - S.stop.values)
        base = okbar[bars] & (risk >= 0.5 * S.atr.values) & (risk <= 4 * S.atr.values) & (risk >= 3 * COST[sym])
        for pat in sorted(S.pattern.unique()):
            for filt in ('none', '1H', '4H'):
                tr = trend[filt][bars]
                m = base & (S.pattern.values == pat) & ((tr == side) if filt != 'none' else True)
                if m.sum() < 15: continue
                b_, s_, j_, e_, r_, tg_ = bars[m], side[m], j[m], e[m], risk[m], S.target.values[m]
                ratm = r_ / A[b_]; st4 = tr[m] if filt != 'none' else np.zeros(m.sum(), int)
                for mode, mh in (('scalp2h', 120), ('intraday8h', 480)):
                    for tgt in ('measured', '1R', '2R'):
                        tp = tg_ if tgt == 'measured' else e_ + s_ * (1 if tgt == '1R' else 2) * r_
                        okt = s_ * (tp - e_) > 0
                        jj_ = np.where(okt, j_, -1)
                        R, hold = sim(o, hh, ll, cc, tod, day, dow, jj_, s_, e_ - s_ * r_, tp, COST[sym], mh, False)
                        R0, _ = sim(o, hh, ll, cc, tod, day, dow, jj_, s_, e_ - s_ * r_, tp, 0.0, mh, False)
                        f = np.isfinite(R)
                        if f.sum() < 15: continue
                        rm = np.where(okt, s_ * (tp - e_) / r_, 1.0)
                        pl = []
                        for rep in range(50):
                            pb = np.empty(len(s_), np.int64)
                            for key_ in set(zip(st4.tolist(), s_.tolist())):
                                mk = (st4 == key_[0]) & (s_ == key_[1]); pb[mk] = rng.choice(pools[filt][key_], mk.sum())
                            jp = np.where(okt, J[pb], -1).astype(np.int64); ep = o[J[pb]]; rk = ratm * A[pb]
                            Rp, _ = sim(o, hh, ll, cc, tod, day, dow, jp, s_, ep - s_ * rk, ep + s_ * rm * rk, 0.0, mh, False)
                            pl.append(np.nanmean(Rp))
                        pl = np.array(pl); rr = R[f]; g0 = np.nanmean(R0[f])
                        yrs = pd.Series(rr, index=lt.index[b_[f]].year).groupby(level=0).sum()
                        rows.append(dict(sym=sym, ltf=LTF, k=k, pattern=pat, filter=filt, mode=mode, target=tgt, n=int(f.sum()),
                                         win=(rr > 0).mean(), meanR=rr.mean(), gross=g0, t=rr.mean() / rr.std(ddof=1) * np.sqrt(len(rr)),
                                         yrs_pos=f'{(yrs > 0).sum()}/{len(yrs)}', placebo_gross=pl.mean(), beats_placebo=(pl < g0).mean()))
                        trades.append(pd.DataFrame(dict(sym=sym, ltf=LTF, k=k, pattern=pat, filter=filt, mode=mode, target=tgt,
                                                        t=lt.index[b_[f]], entry=pd.DatetimeIndex(t1[j_[f]]), hold_min=hold[f], side=s_[f], R=R[f], R0=R0[f])))
tag = LTF.replace('min', 'm')
pd.DataFrame(rows).to_csv(f'out/ac{tag}_summary.csv', index=False)
pd.concat(trades).to_csv(f'out/ac{tag}_trades.csv.gz', index=False)
print('saved', len(rows), 'cells')
