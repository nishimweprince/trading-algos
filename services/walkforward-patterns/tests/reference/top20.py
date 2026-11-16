"""
Reference implementation of the TOP-20 strategies at their selected stop, TP = 2R, 48h max hold (flat Friday 16:45 EST),
one position at a time per strategy, strategies independent. Writes one CSV of trades per strategy (parity reference)
plus a summary. Times are in the research clock: EST fixed UTC-5 (HistData), also given in UTC.

Run from the research folder (needs out/ panels/trade files built by the research scripts):
    python3 top20.py
"""
import numpy as np, pandas as pd, os
from lib import load_m1, resample, entry_index, COST
from hs import sim, CACHE

OUT = 'out/top20'; os.makedirs(OUT, exist_ok=True)
PIP = {'XAUUSD': 0.10, 'USDJPY': 0.01, 'AUDUSD': 0.0001}

# id, name, symbol, stop_pips (best stop from the final compilation)
TOP20 = [
    ('S01', 'Gold double bottom 10M, 1H trend, k5', 'XAUUSD', 80),
    ('S02', 'Gold resistance breakout 15M, 1H trend, k5', 'XAUUSD', 100),
    ('S03', 'Gold inverse H&S 10M, 4H trend, k5', 'XAUUSD', 50),
    ('S04', 'Gold double top 15M, 4H trend, k5', 'XAUUSD', 50),
    ('S05', 'Gold ascending triangle 15M, 1H trend, k5', 'XAUUSD', 100),
    ('S06', 'USDJPY double bottom 15M, 4H trend, k3', 'USDJPY', 30),
    ('S07', 'C4 Gold inverse H&S 15M, 4H up, k3', 'XAUUSD', 80),
    ('S08', 'C6 USDJPY H&S confluence 5/10/15M, 1H, k3', 'USDJPY', 30),
    ('S09', 'USDJPY head & shoulders top 15M, no filter, k3', 'USDJPY', 40),
    ('S10', 'C1 Gold high-vol flush short 15M', 'XAUUSD', 70),
    ('S11', 'AUDUSD double top 15M, 4H trend, k3', 'AUDUSD', 30),
    ('S12', 'C5 USDJPY inverse H&S 5M, 1H up, k5', 'USDJPY', 40),
    ('S13', 'USDJPY ascending triangle 5M, no filter, k3', 'USDJPY', 50),
    ('S14', 'AUDUSD descending triangle 10M, 4H trend, k3', 'AUDUSD', 40),
    ('S15', 'Gold rectangle 15M, 4H trend, k5', 'XAUUSD', 50),
    ('S16', 'AUDUSD head & shoulders top 15M, 1H trend, k3', 'AUDUSD', 30),
    ('S17', 'USDJPY flag 10M, 4H trend, k5', 'USDJPY', 50),
    ('S18', 'C3 Gold inside-day fade short 15M', 'XAUUSD', 100),
    ('S19', 'C2 Gold low-ADX momentum long 15M', 'XAUUSD', 100),
    ('S20', 'USDJPY all-pattern confluence portfolio 5/10/15M, 1H, k5', 'USDJPY', 40),
]

M = {}
def mkt(sym):
    if sym not in M:
        m1 = load_m1(f'{CACHE}/{sym}_m1.npz'); idx = m1.index
        tday = (idx + pd.Timedelta(hours=7)).normalize()
        M[sym] = dict(m1=m1, t1=idx.values, tod=(((idx.hour * 60 + idx.minute).values + 420) % 1440).astype(np.int64),
                      day=tday.values.astype('datetime64[D]').astype(np.int64), dow=tday.dayofweek.values.astype(np.int64),
                      ohlc=[m1[x].values.astype(np.float64) for x in 'ohlc'])
    return M[sym]

def confl(g, key, w=np.timedelta64(60, 'm')):
    g = g.sort_values('entry'); ent, sd, src = g.entry.values, g.side.values, g[key].values; ok = np.zeros(len(g), bool)
    for i in range(len(g)):
        lo = np.searchsorted(ent, ent[i] - w); ok[i] = np.any((sd[lo:i] == sd[i]) & (src[lo:i] != src[i]))
    return g[ok]

# ---------- signal sources
A = pd.concat([pd.read_csv(f'out/ac{x}_trades.csv.gz', parse_dates=['t', 'entry']) for x in ('5m', '10m', '15m')], ignore_index=True)
A['pg'] = A.pattern.replace({'bull_flag': 'flag', 'bear_flag': 'flag'})
def ac(sym, ltf, pg, filt, k, mode):
    g = A[(A.sym == sym) & (A.ltf == ltf) & (A.pg == pg) & (A['filter'] == filt) & (A.k == k) & (A['mode'] == mode) & (A.target == '2R')]
    return g.entry.values, g.side.values, g.t.values
# best setup per pattern x symbol (as in fixedstop_ac.py) -> original hold of that setup decides which signal rows were valid
S = pd.concat([pd.read_csv(f'out/ac{x}_summary.csv') for x in ('5m', '10m', '15m')]); S['pg'] = S.pattern.replace({'bull_flag': 'flag', 'bear_flag': 'flag'})
def best(sym, pg):
    b = S[(S.sym == sym) & (S.pg == pg) & (S.n >= 60)].sort_values('t', ascending=False).iloc[0]
    return ac(sym, b.ltf, pg, b['filter'], int(b.k), b['mode']), (b.ltf, b['filter'], int(b.k), b['mode'])

sig, setup = {}, {}
for sid, sym, pg in [('S01', 'XAUUSD', 'double_bottom'), ('S02', 'XAUUSD', 'resistance'), ('S03', 'XAUUSD', 'inverse_hs'),
                     ('S04', 'XAUUSD', 'double_top'), ('S05', 'XAUUSD', 'ascending_triangle'), ('S06', 'USDJPY', 'double_bottom'),
                     ('S09', 'USDJPY', 'head_shoulders'), ('S11', 'AUDUSD', 'double_top'), ('S13', 'USDJPY', 'ascending_triangle'),
                     ('S14', 'AUDUSD', 'descending_triangle'), ('S15', 'XAUUSD', 'rectangle'), ('S16', 'AUDUSD', 'head_shoulders'),
                     ('S17', 'USDJPY', 'flag')]:
    sig[sid], setup[sid] = best(sym, pg)
# S05 is the hand-selected ascending triangle (15M, 1H, k5, intraday8h) - same as best(); assert
assert setup['S05'] == ('15min', '1H', 5, 'intraday8h'), setup['S05']
# C4
T = pd.read_pickle('out/hs_trades.pkl')
g = T[(T.sym == 'XAUUSD') & (T.costs == 'cost') & (T.side == 1) & (T.trend4 == 1) & (T.k == 3) & (T['mode'] == 'swing48h') & (T.exit == '2R')]
sig['S07'] = ((g.t + pd.Timedelta(minutes=15)).values, g.side.values, g.t.values); setup['S07'] = ('15min', '4H', 3, 'hs.detect')
# C5 / C6
T5 = pd.read_csv('out/hs5m_trades.csv', parse_dates=['t', 'entry'])
g = T5[(T5.sym == 'USDJPY') & (T5.htf == '1H') & (T5.side == 1) & (T5.k == 5) & (T5['mode'] == 'intraday') & (T5.target == '2R')]
sig['S12'] = (g.entry.values, g.side.values, g.t.values); setup['S12'] = ('5min', '1H', 5, 'hs.detect')
TT = pd.concat([pd.read_csv(f'out/hs{x}_trades.csv', parse_dates=['t', 'entry']) for x in ('5m', '10m', '15m')], ignore_index=True)
g = confl(TT[(TT.sym == 'USDJPY') & (TT.htf == '1H') & (TT.k == 3) & (TT['mode'] == 'intraday') & (TT.target == '2R')], 'ltf')
sig['S08'] = (g.entry.values, g.side.values, g.t.values); setup['S08'] = ('5/10/15min', '1H', 3, 'hs.detect confluence')
# S20 portfolio
g = A[(A.sym == 'USDJPY') & (A['filter'] == '1H') & (A.k == 5) & (A['mode'] == 'intraday8h') & (A.target == '2R')].copy()
g['src'] = g.pattern + '@' + g.ltf; g = confl(g, 'src')
sig['S20'] = (g.entry.values, g.side.values, g.t.values); setup['S20'] = ('5/10/15min', '1H', 5, 'all patterns confluence')
# C1-C3 indicator rules
P = pd.read_pickle('out/XAUUSD_panel.pkl'); L, Hc = P['L'], P['H']
m15 = resample(mkt('XAUUSD')['m1'], '15min'); ent15 = entry_index(mkt('XAUUSD')['m1'], m15)
t1x = mkt('XAUUSD')['t1']
for sid, hc, l1, l2, side in [('S10', 'H_vol_hi', 'L_rsi2_lt10', 'L_ext_dn', -1), ('S19', 'H_adx_lt20', 'L_bull_engulf', 'L_ext_up', 1),
                              ('S18', 'D_inside_pd', 'L_ema_xup', 'L_ext_up', -1)]:
    m = (Hc[hc] & L[l1] & L[l2]).values; e = m.copy(); e[1:] &= ~m[:-1]; b = np.where(e & (ent15 >= 0))[0]
    sig[sid] = (t1x[ent15[b]], np.full(len(b), side), m15.index.values[b]); setup[sid] = ('15min', 'rule', None, f'{hc} & {l1} & {l2}')

# ---------- simulate
rows = []
for sid, name, sym, sp in TOP20:
    mk = mkt(sym); o, h, l, c = mk['ohlc']; t1 = mk['t1']
    ent_t, side, sig_t = sig[sid]
    order = np.argsort(ent_t); ent_t, side, sig_t = np.asarray(ent_t)[order], np.asarray(side, np.int64)[order], np.asarray(sig_t)[order]
    j = np.searchsorted(t1, pd.DatetimeIndex(ent_t).values).astype(np.int64)
    X = sp * PIP[sym]; e = o[j]; sl = e - side * X; tp = e + side * 2 * X
    R, hold = sim(o, h, l, c, mk['tod'], mk['day'], mk['dow'], j, side, sl, tp, COST[sym], 2880, True)
    keep, free = [], -1
    for i in range(len(j)):
        if np.isfinite(R[i]) and j[i] >= free: keep.append(i); free = j[i] + hold[i] + 1
    k = np.array(keep)
    px = e[k] + side[k] * (R[k] * X + COST[sym])          # gross exit price (bid series)
    reason = np.where(R[k] > 1.9, 'TP', np.where(R[k] < -0.95, 'SL', 'TIME/FRIDAY'))
    ex_idx = np.minimum(j[k] + np.maximum(hold[k], 1) - 1, len(t1) - 1)
    tr = pd.DataFrame(dict(signal_bar_est=pd.DatetimeIndex(sig_t[k]), entry_time_est=pd.DatetimeIndex(t1[j[k]]),
                           side=np.where(side[k] == 1, 'BUY', 'SELL'), entry=e[k], stop=sl[k], take_profit=tp[k],
                           exit_time_est=pd.DatetimeIndex(t1[ex_idx]), exit_price=px, exit_reason=reason, hold_minutes=hold[k], R_net=R[k]))
    tr['entry_time_utc'] = tr.entry_time_est + pd.Timedelta(hours=5); tr['exit_time_utc'] = tr.exit_time_est + pd.Timedelta(hours=5)
    tr.to_csv(f'{OUT}/{sid}_trades.csv', index=False, float_format='%.5f')
    months = len(pd.period_range('2020-01', pd.Timestamp(t1[-1]).strftime('%Y-%m'), freq='M'))
    r = R[k]; yrs = pd.Series(r, index=tr.entry_time_est.dt.year).groupby(level=0).sum()
    rows.append(dict(id=sid, name=name, symbol=sym, setup=str(setup[sid]), stop_pips=sp, tp_pips=2 * sp, trades=len(r), per_month=round(len(r) / months, 1),
                     win=round((r > 0).mean(), 3), meanR=round(r.mean(), 3), t=round(r.mean() / r.std(ddof=1) * np.sqrt(len(r)), 2),
                     net_pips=round((r * sp).sum(), 1), yrs_pos=f'{(yrs > 0).sum()}/{len(yrs)}'))
D = pd.DataFrame(rows); D.to_csv(f'{OUT}/top20_summary.csv', index=False)
pd.set_option('display.width', 250, 'display.max_colwidth', 60); print(D.to_string(index=False))
