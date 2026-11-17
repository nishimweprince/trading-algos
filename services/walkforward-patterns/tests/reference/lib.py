"""
Walk-forward alpha search library: bars, conditions, M1-path trade simulator.

Time convention: HistData timestamps are EST with no DST (fixed UTC-5).
Trading day runs 17:00 -> 17:00 EST (gold re-opens 18:00). Every trade is
flat by 16:45 EST on the day it was opened (intraday only).
"""
import numpy as np, pandas as pd
from numba import njit

import os
COST = {'XAUUSD': float(os.environ.get('XAU_COST', 0.30)), 'AUDUSD': 0.00008, 'USDJPY': 0.008}   # round trip, price units
EXITS = [(1.0, 1.0), (1.5, 1.0), (1.0, 1.5), (1.0, 2.0), (2.0, 2.0), (0.75, 1.5)]  # (SL, TP) in ATR15 multiples
MAX_HOLD_MIN = 480          # 8 hours
EOD_MIN = (16 * 60 + 45 + 7 * 60) % 1440  # force flat 16:45 EST, in minutes since 17:00 EST day start
NO_ENTRY = (16 * 60, 19 * 60)  # no new entries 16:00-18:59 EST (pre-close + rollover spread)


def load_m1(path):
    z = np.load(path)
    t = pd.to_datetime(z['t'].astype('datetime64[m]'))
    return pd.DataFrame({'o': z['o'], 'h': z['h'], 'l': z['l'], 'c': z['c']}, index=t)


def resample(m1, rule):
    return m1.resample(rule, label='left', closed='left').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last'}).dropna()


# ---------------- indicators ----------------
def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def wilder(s, n): return s.ewm(alpha=1 / n, adjust=False).mean()

def atr(df, n=14):
    pc = df.c.shift()
    tr = pd.concat([df.h - df.l, (df.h - pc).abs(), (df.l - pc).abs()], axis=1).max(axis=1)
    return wilder(tr, n)

def rsi(c, n):
    d = c.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

def adx(df, n=14):
    up, dn = df.h.diff(), -df.l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * wilder(pd.Series(pdm, df.index), n) / a
    ndi = 100 * wilder(pd.Series(ndm, df.index), n) / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    return wilder(dx, n)


def ltf_conditions(d):
    """15M trigger conditions, all known at the close of the bar."""
    C = {}
    o, h, l, c = d.o, d.h, d.l, d.c
    a = atr(d)
    body = (c - o).abs(); rng = (h - l).replace(0, np.nan)
    uw = h - np.maximum(o, c); lw = np.minimum(o, c) - l
    po, pc = o.shift(), c.shift()
    bull, bear = c > o, c < o
    C['L_bull_engulf'] = bull & (pc < po) & (c >= po) & (o <= pc)
    C['L_bear_engulf'] = bear & (pc > po) & (c <= po) & (o >= pc)
    C['L_hammer'] = (lw > 2 * body) & (uw < body) & (rng > 0.6 * a)
    C['L_shooting'] = (uw > 2 * body) & (lw < body) & (rng > 0.6 * a)
    C['L_inside'] = (h < h.shift()) & (l > l.shift())
    C['L_outside'] = (h > h.shift()) & (l < l.shift())
    C['L_bigbull'] = bull & (body > 1.2 * a)
    C['L_bigbear'] = bear & (body > 1.2 * a)
    C['L_3up'] = (c > pc) & (pc > c.shift(2)) & (c.shift(2) > c.shift(3))
    C['L_3down'] = (c < pc) & (pc < c.shift(2)) & (c.shift(2) < c.shift(3))
    r14, r2 = rsi(c, 14), rsi(c, 2)
    C['L_rsi14_lt30'] = r14 < 30; C['L_rsi14_gt70'] = r14 > 70
    C['L_rsi2_lt10'] = r2 < 10;   C['L_rsi2_gt90'] = r2 > 90
    e20, e50 = ema(c, 20), ema(c, 50)
    C['L_c_gt_ema20'] = c > e20; C['L_c_lt_ema20'] = c < e20
    C['L_ema_xup'] = (e20 > e50) & (e20.shift() <= e50.shift())
    C['L_ema_xdn'] = (e20 < e50) & (e20.shift() >= e50.shift())
    m, sd = c.rolling(20).mean(), c.rolling(20).std()
    C['L_bb_below'] = c < m - 2 * sd; C['L_bb_above'] = c > m + 2 * sd
    hh, ll = h.rolling(20).max().shift(), l.rolling(20).min().shift()
    C['L_don_break_hi'] = c > hh; C['L_don_break_lo'] = c < ll
    C['L_sweep_lo'] = (l < ll) & (c > ll); C['L_sweep_hi'] = (h > hh) & (c < hh)
    k = 100 * (c - l.rolling(14).min()) / (h.rolling(14).max() - l.rolling(14).min())
    C['L_stoch_lt20'] = k < 20; C['L_stoch_gt80'] = k > 80
    C['L_ext_up'] = (c - e50) > 2 * a; C['L_ext_dn'] = (c - e50) < -2 * a
    mh = ema(c, 12) - ema(c, 26); mh = mh - ema(mh, 9)
    C['L_macd_up'] = (mh > 0) & (mh.shift() <= 0); C['L_macd_dn'] = (mh < 0) & (mh.shift() >= 0)
    C['L_nr7'] = (h - l) <= (h - l).rolling(7).min()
    return pd.DataFrame(C).fillna(False).astype(bool), a


def htf_conditions(H, D):
    """1H context conditions on a closed 1H bar (plus prior-day levels)."""
    C = {'H_any': pd.Series(True, H.index)}
    c = H.c; e20, e50 = ema(c, 20), ema(c, 50)
    C['H_above_ema50'] = c > e50; C['H_below_ema50'] = c < e50
    C['H_ema_bull'] = e20 > e50;  C['H_ema_bear'] = e20 < e50
    C['H_slope_up'] = e50 > e50.shift(5); C['H_slope_dn'] = e50 < e50.shift(5)
    r = rsi(c, 14)
    C['H_rsi_lt30'] = r < 30; C['H_rsi_30_45'] = (r >= 30) & (r < 45); C['H_rsi_45_55'] = (r >= 45) & (r < 55)
    C['H_rsi_55_70'] = (r >= 55) & (r < 70); C['H_rsi_gt70'] = r >= 70
    ad = adx(H); C['H_adx_gt25'] = ad > 25; C['H_adx_lt20'] = ad < 20
    hh, ll = H.h.rolling(20).max(), H.l.rolling(20).min(); pos = (c - ll) / (hh - ll)
    C['H_range_top'] = pos > 0.8; C['H_range_bot'] = pos < 0.2
    C['H_bull_bar'] = c > H.o; C['H_bear_bar'] = c < H.o
    m, sd = c.rolling(20).mean(), c.rolling(20).std()
    C['H_bb_above'] = c > m + 2 * sd; C['H_bb_below'] = c < m - 2 * sd
    va = atr(H, 14) / atr(H, 100); C['H_vol_hi'] = va > 1.25; C['H_vol_lo'] = va < 0.8
    out = pd.DataFrame(C)
    # prior trading-day high/low (trading day = EST + 7h date)
    tday = (H.index + pd.Timedelta(hours=7)).normalize()
    pdh = D.h.shift(); pdl = D.l.shift()
    ph, pl = pdh.reindex(tday).values, pdl.reindex(tday).values
    out['D_above_pdh'] = c.values > ph; out['D_below_pdl'] = c.values < pl
    out['D_inside_pd'] = (c.values <= ph) & (c.values >= pl)
    return out.fillna(False).astype(bool)


def build(m1):
    """Return 15M frame with LTF+HTF conditions aligned without look-ahead, plus ATR15."""
    m15, h1 = resample(m1, '15min'), resample(m1, '1h')
    tday_m1 = m1.copy(); tday_m1.index = tday_m1.index + pd.Timedelta(hours=7)
    D = resample(tday_m1, '1D'); D.index = D.index.normalize()
    L, a15 = ltf_conditions(m15)
    Hc = htf_conditions(h1, D)
    # 15M bar starting s closes at s+15m; latest closed 1H bar = floor(s+15m, 1h) - 1h
    key = (m15.index + pd.Timedelta(minutes=15)).floor('1h') - pd.Timedelta(hours=1)
    Hal = Hc.reindex(key).fillna(False).astype(bool); Hal.index = m15.index
    return m15, L, Hal, a15


# ---------------- simulator ----------------
@njit(cache=True)
def _sim(o, h, l, c, tod, day, ent_idx, atrv, side, sl_k, tp_k, cost, max_hold, eod):
    n = ent_idx.shape[0]
    R = np.full(n, np.nan); hold = np.zeros(n, np.int32)
    for i in range(n):
        j = ent_idx[i]
        if j < 0 or not (atrv[i] > 0):
            continue
        e = o[j]; risk = sl_k * atrv[i]
        sl = e - side * risk; tp = e + side * tp_k * atrv[i]
        d0 = day[j]; px = np.nan; k = j
        while True:
            if k >= o.shape[0] or day[k] != d0 or tod[k] >= eod or k - j >= max_hold:
                px = c[k - 1] if k > j else e; break
            if side == 1:
                if o[k] <= sl: px = o[k]; break
                if l[k] <= sl: px = sl; break
                if h[k] >= tp: px = tp; break
            else:
                if o[k] >= sl: px = o[k]; break
                if h[k] >= sl: px = sl; break
                if l[k] <= tp: px = tp; break
            k += 1
        R[i] = (side * (px - e) - cost) / risk
        hold[i] = k - j
    return R, hold


def entry_index(m1, m15):
    """M1 index of entry (open of first minute after the 15M bar closes), -1 if disallowed."""
    t1 = m1.index.values; close_t = (m15.index + pd.Timedelta(minutes=15)).values
    j = np.searchsorted(t1, close_t)
    ok = j < len(t1)
    jj = np.where(ok, j, 0)
    et = pd.DatetimeIndex(t1[jj]); tod = et.hour * 60 + et.minute
    sig_day = ((m15.index + pd.Timedelta(hours=7)).normalize()).values
    ent_day = ((et + pd.Timedelta(hours=7)).normalize()).values
    ok &= ~((tod >= NO_ENTRY[0]) & (tod < NO_ENTRY[1]))
    ok &= (sig_day == ent_day) & ((t1[jj] - close_t) <= np.timedelta64(30, 'm'))
    return np.where(ok, j, -1)


def outcomes(m1, m15, a15, sym):
    """R-multiple outcome for every 15M bar x direction x exit config. Shape (N, 2*len(EXITS))."""
    idx = m1.index
    tod = (((idx.hour * 60 + idx.minute).values + 7 * 60) % 1440).astype(np.int32)  # minutes since 17:00 EST
    day = ((idx + pd.Timedelta(hours=7)).normalize().values.astype('datetime64[D]').astype(np.int64))
    ent = entry_index(m1, m15).astype(np.int64)
    o, h, l, c = (m1[x].values.astype(np.float64) for x in 'ohlc')
    av = a15.values.astype(np.float64)
    cols, names, holds = [], [], []
    for side in (1, -1):
        for sl, tp in EXITS:
            R, hd = _sim(o, h, l, c, tod, day, ent, av, side, sl, tp, COST[sym], MAX_HOLD_MIN, EOD_MIN)
            cols.append(R); holds.append(hd); names.append(f"{'L' if side == 1 else 'S'}_sl{sl}_tp{tp}")
    return np.column_stack(cols), names, np.column_stack(holds)
