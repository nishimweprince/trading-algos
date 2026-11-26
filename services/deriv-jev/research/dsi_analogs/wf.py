"""Tick-level walk-forward of non-DSI event patterns on Deriv synthetics. Read-only, offline.

Data: real Deriv MT5 bid/ask ticks on a 1-second grid (last tick in each second; seconds whose last tick is
older than 5 s are invalid). Every pattern is an EVENT (time t, side s); a trade enters at t+1 s (latency) at the
ask (long) / bid (short) and exits after a fixed horizon at the bid / ask. One open trade per rule. Net bps include
the spread actually quoted and pro-rata swap; gross bps are mid-to-mid. No stops, no bar simulation.

Families (none are the DSI rules mom/brk/rsi/ema on bars with ATR stops):
  spk{k}       spike (|1 s return| > k robust sigma), side = spike direction
  post{T}_{all|flip}  T s after a spike, side = sign of the jump-free drift since the spike
                      (flip = only when that drift sign differs from the 300 s pre-spike drift: a phase flip)
  carry_{b}    every 60 s, side = sign of the 600 s jump-free drift, when time since the last spike is in bucket b
  run{n}       n consecutive same-direction 1 s moves
  brk{N}       first cross of the prior N-s extreme (>= 60 s apart); retest{N}: first return to that border within 600 s
  vshock{q}    60 s realised vol (jump-free) crosses q x the 1800 s level, side = sign of the 60 s move
  vcalm        that ratio crosses below 1/3, side = sign of the 300 s drift
  hour{h}      start of UTC hour h, side long, 3600 s horizon only (calendar control)
Each family is tested as follow (s) and fade (-s), at horizons 30/120/600/1800 s.

Walk-forward: test weeks from day 28, train = the 28 days before. A rule is selected for a test week if, on its
train window, n >= 40, mean net > 0 and daily t >= 3 (top 5 by t). OOS = their test-week trades.
Usage: wf.py CODE [CODE ...]  -> /opt/dsi-analogs/wf/out/CODE.json"""
import json, os, sys, time
import numpy as np
from numba import njit

D = os.environ.get('WF_DIR', '/opt/dsi-analogs/wf'); OUT = os.environ.get('WF_OUT', 'out'); os.makedirs(f'{D}/{OUT}', exist_ok=True)
TMIN = float(os.environ.get('WF_TMIN', '3'))     # walk-forward selection: train daily t >= TMIN
H = (30, 120, 600, 1800); LAT = 1; TRAIN, TEST, START = 28, 7, 28


@njit(cache=True)
def grid(ms, s0, S):
    idx = np.full(S, -1, np.int64); j = 0; n = len(ms)
    for k in range(S):
        end = (s0 + k) * 1000 + 999
        while j < n and ms[j] <= end:
            j += 1
        idx[k] = j - 1
    return idx


@njit(cache=True)
def runlen(r):
    out = np.zeros(len(r), np.int64)
    for i in range(1, len(r)):
        if r[i] != 0 and (r[i] > 0) == (r[i - 1] > 0) and r[i - 1] != 0:
            out[i] = out[i - 1] + 1
        elif r[i] != 0:
            out[i] = 1
    return out


@njit(cache=True)
def prior_ext(x, N, sign):
    """sign=+1: max of x[t-N .. t-1]; sign=-1: min. Monotonic deque."""
    n = len(x); out = np.full(n, np.nan); dq = np.empty(n, np.int64); h = 0; t = 0
    for i in range(n):
        if i > 0:
            j = i - 1; v = sign * x[j]
            while t > h and sign * x[dq[t - 1]] <= v:
                t -= 1
            dq[t] = j; t += 1
            while dq[h] < i - N:
                h += 1
            if i >= N:
                out[i] = x[dq[h]]
    return out


@njit(cache=True)
def breaks(mid, pmax, pmin, gap):
    ts = []; ss = []; lu = -10 ** 9; ld = -10 ** 9
    for i in range(1, len(mid)):
        if not np.isnan(pmax[i]) and mid[i] > pmax[i] and not (mid[i - 1] > pmax[i - 1]) and i - lu >= gap:
            ts.append(i); ss.append(1); lu = i
        elif not np.isnan(pmin[i]) and mid[i] < pmin[i] and not (mid[i - 1] < pmin[i - 1]) and i - ld >= gap:
            ts.append(i); ss.append(-1); ld = i
    return np.array(ts, np.int64), np.array(ss, np.int64)


@njit(cache=True)
def retests(mid, bt, bs, level, win):
    ts = []; ss = []
    for j in range(len(bt)):
        t0 = bt[j]; B = level[j]
        for i in range(t0 + 1, min(t0 + win, len(mid))):
            if (bs[j] > 0 and mid[i] <= B) or (bs[j] < 0 and mid[i] >= B):
                ts.append(i); ss.append(bs[j]); break
    return np.array(ts, np.int64), np.array(ss, np.int64)


@njit(cache=True)
def trade(t, s, h, bid, ask, mid, valid, swap_bps_s):
    n = len(t); S = len(bid)
    te = np.full(n, -1, np.int64); net = np.zeros(n); gross = np.zeros(n); busy = -1
    for j in range(n):
        e = t[j] + 1; x = e + h
        if e <= busy or x >= S or not valid[e] or not valid[x]:
            continue
        if s[j] > 0:
            pe = ask[e]; px = bid[x]
        else:
            pe = bid[e]; px = ask[x]
        net[j] = s[j] * (px - pe) / pe * 1e4 - swap_bps_s * h
        gross[j] = s[j] * (mid[x] - mid[e]) / mid[e] * 1e4
        te[j] = e; busy = x
    return te, net, gross


def events(mid, valid, r, rj, Lj, sig, s0):
    E = {}
    S = len(mid)
    for k in (8, 20):
        t = np.flatnonzero(np.abs(r) > k * sig); E[f'spk{k}'] = (t, np.sign(r[t]).astype(np.int64))
    sp = np.flatnonzero(np.abs(r) > 8 * sig)
    for T in (30, 120, 300):
        t = sp[(sp + T < S) & (sp >= 300)]
        post = np.sign(Lj[t + T] - Lj[t]); pre = np.sign(Lj[t] - Lj[t - 300])
        m = post != 0
        E[f'post{T}_all'] = (t[m] + T, post[m].astype(np.int64))
        m2 = m & (pre != 0) & (pre != post)
        E[f'post{T}_flip'] = (t[m2] + T, post[m2].astype(np.int64))
    samp = np.arange(600, S, 60)
    li = np.searchsorted(sp, samp, 'right') - 1
    since = np.where(li >= 0, samp - sp[np.clip(li, 0, None)], 10 ** 9) if len(sp) else np.full(len(samp), 10 ** 9)
    side = np.sign(Lj[samp] - Lj[samp - 600]).astype(np.int64)
    for name, lo, hi in (('lt60', 0, 60), ('60_600', 60, 600), ('600_3600', 600, 3600), ('gt3600', 3600, 10 ** 10)):
        m = (since >= lo) & (since < hi) & (side != 0) & valid[samp]
        E[f'carry_{name}'] = (samp[m], side[m])
    rl = runlen(np.where(valid, r, 0.0))
    for n in (4, 6, 8):
        t = np.flatnonzero(rl == n); E[f'run{n}'] = (t, np.sign(r[t]).astype(np.int64))
    for N in (900, 3600):
        pmax = prior_ext(mid, N, 1); pmin = prior_ext(mid, N, -1)
        bt, bs = breaks(mid, pmax, pmin, 60)
        E[f'brk{N}'] = (bt, bs)
        lvl = np.where(bs > 0, pmax[bt], pmin[bt])
        E[f'retest{N}'] = retests(mid, bt, bs, lvl, 600)
    c2 = np.cumsum(rj ** 2)
    rv60 = np.r_[np.full(60, np.nan), (c2[60:] - c2[:-60]) / 60]
    rv1800 = np.r_[np.full(1860, np.nan), (c2[1800:-60] - c2[:-1860]) / 1800]   # the 1800 s before the 60 s window
    ratio = rv60 / np.maximum(rv1800, 1e-30)
    prev = np.r_[np.nan, ratio[:-1]]
    for q in (3, 6):
        t = np.flatnonzero((ratio > q) & ~(prev > q)); t = t[t >= 300]
        sd = np.sign(Lj[t] - Lj[t - 60]).astype(np.int64); m = sd != 0
        E[f'vshock{q}'] = (t[m], sd[m])
    t = np.flatnonzero((ratio < 1 / 3) & ~(prev < 1 / 3)); t = t[t >= 300]
    sd = np.sign(Lj[t] - Lj[t - 300]).astype(np.int64); m = sd != 0
    E['vcalm'] = (t[m], sd[m])
    hrs = np.flatnonzero(((s0 + np.arange(S)) % 3600) == 0)
    for hh in range(24):
        t = hrs[((s0 + hrs) // 3600) % 24 == hh]; E[f'hour{hh:02d}'] = (t, np.ones(len(t), np.int64))
    return E


def daily_t(x):
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0


def run(code):
    t0 = time.time()
    z = np.load(f'{D}/ticks/{code}.npz'); spec = json.load(open(f'{D}/ticks/{code}.json'))
    ms, bid, ask = z['ms'], z['bid'], z['ask']
    ok = (bid > 0) & (ask > 0) & (ask >= bid); ms, bid, ask = ms[ok], bid[ok], ask[ok]
    o = np.argsort(ms, kind='stable'); ms, bid, ask = ms[o], bid[o], ask[o]
    s0 = int(ms[0] // 1000 // 86400 * 86400 + 86400)          # first full UTC day
    S = int(ms[-1] // 1000) - s0 + 1
    idx = grid(ms, s0, S)
    valid = idx >= 0
    age = (s0 + np.arange(S)) * 1000 + 999 - ms[np.clip(idx, 0, None)]
    valid &= age < 5000
    bid_g = bid[np.clip(idx, 0, None)]; ask_g = ask[np.clip(idx, 0, None)]; mid = (bid_g + ask_g) / 2
    lm = np.log(mid); r = np.r_[0.0, np.diff(lm)]; r[~valid] = 0; r[np.r_[False, ~valid[:-1]]] = 0
    nz = r[r != 0]; sig = float(np.median(np.abs(nz)) * 1.4826) if len(nz) else 1e-9
    rj = np.where(np.abs(r) > 8 * sig, 0.0, r); Lj = np.cumsum(rj)
    price = float(np.median(mid[valid]))
    if spec['swap_mode'] == 5:
        swap = abs(spec['swap_long']) / 100 / 31536000 * 1e4
    elif spec['swap_mode'] == 1:
        swap = abs(spec['swap_long']) * spec['point'] / price / 86400 * 1e4
    else:
        swap = 0.0
    E = events(mid, valid, r, rj, Lj, sig, s0)
    ndays = S // 86400
    spr = (ask_g - bid_g)[valid] / mid[valid] * 1e4
    rules = {}
    for fam, (t, s) in E.items():
        o = np.argsort(t, kind='stable'); t, s = t[o], s[o]      # trade() needs time order (else selection peeks ahead)
        hs = (3600,) if fam.startswith('hour') else H
        for h in hs:
            for mode, sg in (('follow', 1), ('fade', -1)):
                if fam.startswith('hour') and mode == 'fade':
                    continue
                te, net, gross = trade(t, s * sg, h, bid_g, ask_g, mid, valid, swap)
                m = te >= 0
                day = te[m] // 86400
                rules[f'{fam}|{mode}|{h}'] = dict(day=day, net=net[m], gross=gross[m])
    # per rule x day matrices
    names = list(rules)
    NS = np.zeros((len(names), ndays)); GS = np.zeros_like(NS); CN = np.zeros_like(NS)
    for i, k in enumerate(names):
        d = rules[k]['day']; m = d < ndays
        NS[i] = np.bincount(d[m], rules[k]['net'][m], ndays); GS[i] = np.bincount(d[m], rules[k]['gross'][m], ndays)
        CN[i] = np.bincount(d[m], None, ndays)
    # full-sample stats + weekly consistency
    stats = {}
    nweeks = ndays // 7
    for i, k in enumerate(names):
        n = CN[i].sum()
        if n < 30:
            continue
        wk_n = CN[i, :nweeks * 7].reshape(nweeks, 7).sum(1); wk_net = NS[i, :nweeks * 7].reshape(nweeks, 7).sum(1)
        wk_g = GS[i, :nweeks * 7].reshape(nweeks, 7).sum(1); okw = wk_n >= 10
        mn, mg = NS[i].sum() / n, GS[i].sum() / n
        cons_n = float(np.mean(np.sign(wk_net[okw]) == np.sign(mn))) if okw.any() else 0.0
        cons_g = float(np.mean(np.sign(wk_g[okw]) == np.sign(mg))) if okw.any() else 0.0
        stats[k] = dict(n=int(n), net=round(mn, 3), gross=round(mg, 3), t_net=round(daily_t(NS[i]), 2),
                        t_gross=round(daily_t(GS[i]), 2), cons_net=round(cons_n, 2), cons_gross=round(cons_g, 2),
                        weeks=int(okw.sum()))
    # walk-forward
    wf = []; oos_day = np.zeros(ndays); oos_n = 0; oos_sum = 0.0
    for st in range(START, ndays - TEST + 1, TEST):
        tr = slice(st - TRAIN, st); te_ = slice(st, st + TEST)
        cand = []
        for i in range(len(names)):
            n = CN[i, tr].sum()
            if n < 40:
                continue
            mt = NS[i, tr].sum() / n; tt = daily_t(NS[i, tr])
            if mt > 0 and tt >= TMIN:
                cand.append((tt, i))
        cand = sorted(cand, reverse=True)[:5]
        wk = dict(start_day=st, picks=[])
        for tt, i in cand:
            n = int(CN[i, te_].sum()); sm = float(NS[i, te_].sum())
            wk['picks'].append(dict(rule=names[i], train_t=round(tt, 2), train_net=round(NS[i, tr].sum() / CN[i, tr].sum(), 3),
                                    oos_n=n, oos_net=round(sm / n, 3) if n else None))
            oos_day[te_] += NS[i, te_]; oos_n += n; oos_sum += sm
        wf.append(wk)
    test_days = oos_day[START:START + len(wf) * TEST]
    res = dict(code=code, name=spec['name'], days=ndays, price=price, sigma_bps=round(sig * 1e4, 3),
               spread_bps_mean=round(float(spr.mean()), 3), spread_bps_p95=round(float(np.percentile(spr, 95)), 3),
               swap_bps_per_hour=round(swap * 3600, 4), n_spikes8=int((np.abs(r) > 8 * sig).sum()),
               n_rules=len(names), stats=stats, wf=wf,
               wf_summary=dict(weeks=len(wf), weeks_with_picks=sum(1 for w in wf if w['picks']), oos_n=oos_n,
                               oos_net_per_trade=round(oos_sum / oos_n, 3) if oos_n else None,
                               oos_daily_t=round(daily_t(test_days), 2) if oos_n else None,
                               oos_total_bps=round(float(test_days.sum()), 1),
                               weeks_pos=sum(1 for k in range(len(wf)) if test_days[k * TEST:(k + 1) * TEST].sum() > 0)),
               secs=round(time.time() - t0, 1))
    json.dump(res, open(f'{D}/{OUT}/{code}.json', 'w'))
    s = res['wf_summary']
    print(f"{code:8} days {ndays} spread {res['spread_bps_mean']:.2f}bp spikes {res['n_spikes8']} rules {len(names)} | "
          f"WF weeks {s['weeks']} picked {s['weeks_with_picks']} oos n {oos_n} net/trade {s['oos_net_per_trade']} t {s['oos_daily_t']} "
          f"pos weeks {s['weeks_pos']} | {res['secs']}s", flush=True)


if __name__ == '__main__':
    for code in sys.argv[1:]:
        run(code)
