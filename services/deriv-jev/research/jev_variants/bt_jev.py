"""Tick-level replay of the paper trader's former-Jev group, and variants for paper-report items 2-4 (2026-11-27).

Baseline = deriv_paper.py exactly: signals from rules.generic_rules on M1 bid bars (engine.resample/atr); entry at the
first tick >= bar close + 2 s (the paper step runs ~1-3 s after the minute) at the ask (buy) / bid (sell);
R = max(stop_atr x ATR, 1.2 x broker min stop + entry spread); no target; per tick: stop, then the time limit
(open + (max_hold + 1) min), then the trail (best - trail_R x R once best is 1R in favour); one position per
strategy; swap -7.5%/yr pro rata. Exits at the bid (long) / ask (short).

Variants:
  item 2  trail_R in {1.5, 2, 2.5, 3}, max_hold in {480, 960, 1440, none (7-day cap)} minutes
  item 3  stop: 'widen' (baseline) or 'skip' signals whose natural stop is below the broker minimum
  item 4  spread filter: skip entries whose spread < q x the median 1-minute spread over the prior 60 min, q in {0.9, 1.0, 1.1}
Split: in-sample = first half of the days, out-of-sample = second half. Usage: bt_jev.py [validate]
Run with /opt/dsi-analogs/.venv (numba). Ticks from pull_dsi.py in /opt/dsi-analogs/jev/ticks."""
import json, os, sys, itertools, sqlite3
import numpy as np
from numba import njit
sys.path.insert(0, '/opt/trading-algos/services/deriv-jev')
from engine import resample, atr
from rules import generic_rules

D = '/opt/dsi-analogs/jev'
CFG = json.load(open('/opt/trading-algos/services/deriv-jev/config.json'))
STRATS = [s for s in CFG['strategies'] if s.get('group') == 'jev']
SWAP_MIN = 0.075 / 525600
TRAILS = (1.5, 2.0, 2.5, 3.0); HOLDS = (480, 960, 1440, 10080); STOPS = ('widen', 'skip'); SPREADQ = (0.0, 0.9, 1.0, 1.1)
ENTRY_LAG_MS = 2000


@njit(cache=True)
def sim(ms, bid, ask, ei, side, R, take, trR, hold_min, swap_min):
    n = len(ms); out = np.full(len(ei), np.nan); why = np.zeros(len(ei), np.int8); busy = -1
    for j in range(len(ei)):
        e = ei[j]
        if not take[j] or e >= n or ms[e] <= busy:
            continue
        s = side[j]; r0 = R[j]
        px = ask[e] if s > 0 else bid[e]
        sl = px - s * r0; best = px; deadline = ms[e] + (hold_min + 1) * 60000
        i = e + 1; q = px; w = 0
        while i < n:
            q = bid[i] if s > 0 else ask[i]
            if s * (q - sl) <= 0:
                w = 1 if s * (sl - px) < 0 else 2; break        # 1 stop, 2 trail
            if ms[i] >= deadline:
                w = 3; break                                    # time
            if s > 0:
                if q > best:
                    best = q
            else:
                if q < best:
                    best = q
            if s * (best - px) >= r0:
                nsl = best - s * trR * r0
                if s > 0:
                    if nsl > sl:
                        sl = nsl
                else:
                    if nsl < sl:
                        sl = nsl
            i += 1
        if i >= n:
            break
        held = (ms[i] - ms[e]) / 60000.0
        out[j] = (s * (q - px) - swap_min * px * held) / r0; why[j] = w; busy = ms[i]
    return out, why


def load(code):
    z = np.load(f'{D}/ticks/{code}.npz'); spec = json.load(open(f'{D}/ticks/{code}.json'))
    ms, bid, ask = z['ms'], z['bid'], z['ask']
    ok = (bid > 0) & (ask > 0) & (ask >= bid); ms, bid, ask = ms[ok], bid[ok], ask[ok]
    o = np.argsort(ms, kind='stable'); ms, bid, ask = ms[o], bid[o], ask[o]
    mins = ms // 60000; st = np.flatnonzero(np.r_[True, mins[1:] != mins[:-1]]); en = np.r_[st[1:], len(ms)]
    t = mins[st]; O = bid[st]; C = bid[en - 1]; Hh = np.maximum.reduceat(bid, st); Ll = np.minimum.reduceat(bid, st)
    spm = np.add.reduceat(ask - bid, st) / (en - st)                       # mean spread per minute
    return dict(ms=ms, bid=bid, ask=ask, t=t, o=O, h=Hh, l=Ll, c=C, spm=spm, spec=spec)


def signals(X, s):
    tf = s['tf']
    b = resample(X['t'], X['o'], X['h'], X['l'], X['c'], tf, int(X['t'][-1]) + 1); a = atr(b)
    sig = generic_rules(b, a)[s['rule']]
    k = np.flatnonzero((sig != 0) & (np.arange(len(sig)) >= 300) & np.isfinite(a) & (a > 0))
    close_min = b['t'][k] + tf
    ei = np.searchsorted(X['ms'], close_min * 60000 + ENTRY_LAG_MS)
    keep = ei < len(X['ms']); k, ei, close_min = k[keep], ei[keep], close_min[keep]
    spread = X['ask'][ei] - X['bid'][ei]
    nat = s['stop_atr'] * a[k]; R = np.maximum(nat, X['spec']['min_stop'] * 1.2 + spread)
    # median 1-minute spread over the 60 minutes before the bar close
    pos = np.searchsorted(X['t'], close_min)                                # first minute >= close
    med = np.array([np.median(X['spm'][max(0, p - 60):p]) if p > 0 else np.nan for p in pos])
    return dict(ei=ei.astype(np.int64), side=sig[k].astype(np.int64), R=R, widened=R > nat + 1e-12, rel=spread / med,
                entry_ms=X['ms'][ei])


def daily_t(day_sums):
    x = np.asarray(day_sums, float)
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0


def stats(r, ems, d0, lo, hi):
    m = np.isfinite(r) & (ems >= lo) & (ems < hi); x = r[m]
    ndays = int((hi - lo) // 86400000)
    if len(x) == 0:
        return dict(n=0, mean=0.0, total=0.0, win=0.0, t=0.0)
    day = ((ems[m] - lo) // 86400000).astype(int)
    ds = np.bincount(day, x, max(ndays, day.max() + 1))
    return dict(n=int(len(x)), mean=round(float(x.mean()), 3), total=round(float(x.sum()), 1), win=round(float((x > 0).mean()), 3),
                t=round(daily_t(ds), 2))


def main():
    data = {c: load(c) for c in sorted({s['code'] for s in STRATS})}
    t0 = max(int(X['ms'][0]) for X in data.values()); t1 = min(int(X['ms'][-1]) for X in data.values())
    d0 = (t0 // 86400000 + 1) * 86400000; d1 = t1 // 86400000 * 86400000; mid = d0 + (d1 - d0) // 2 // 86400000 * 86400000
    print(f'sample {np.datetime64(d0, "ms")} .. {np.datetime64(d1, "ms")}  split {np.datetime64(mid, "ms")}')
    SIG = {s['tag']: signals(data[s['code']], s) for s in STRATS}
    for s in STRATS:
        g = SIG[s['tag']]; m = (g['entry_ms'] >= d0) & (g['entry_ms'] < d1)
        print(f"  {s['tag']:30} signals {m.sum():5}  widened {g['widened'][m].mean():.0%}  median rel spread {np.nanmedian(g['rel'][m]):.2f}")
    rows = []; keep_trades = {}
    for trR, hold, stop, q in itertools.product(TRAILS, HOLDS, STOPS, SPREADQ):
        per = {}; allr = []; alle = []; why_all = []
        for s in STRATS:
            X = data[s['code']]; g = SIG[s['tag']]
            take = np.ones(len(g['ei']), np.bool_)
            if stop == 'skip':
                take &= ~g['widened']
            if q > 0:
                take &= ~(np.nan_to_num(g['rel'], nan=9.0) < q)
            r, why = sim(X['ms'], X['bid'], X['ask'], g['ei'], g['side'], g['R'], take, trR, hold, SWAP_MIN)
            per[s['tag']] = (stats(r, g['entry_ms'], d0, d0, mid), stats(r, g['entry_ms'], d0, mid, d1))
            allr.append(r); alle.append(g['entry_ms']); why_all.append(why)
            if (trR, hold, stop, q) == (1.5, 480, 'widen', 0.0):
                keep_trades[s['tag']] = (g['entry_ms'], r, why, g['side'])
        R_ = np.concatenate(allr); E_ = np.concatenate(alle); W_ = np.concatenate(why_all)
        IS, OOS = stats(R_, E_, d0, d0, mid), stats(R_, E_, d0, mid, d1)
        m = np.isfinite(R_) & (E_ >= d0) & (E_ < d1)
        time_share = float(R_[m & (W_ == 3)].sum() / max(abs(R_[m]).sum(), 1e-9))
        rows.append(dict(trail=trR, hold=hold, stop=stop, q=q, IS=IS, OOS=OOS, time_exits=round(float((W_[m] == 3).mean()), 3),
                         per={k: v for k, v in per.items()}))
    json.dump(dict(d0=d0, d1=d1, mid=mid, rows=rows), open(f'{D}/bt_jev.json', 'w'))
    np.savez(f'{D}/baseline_trades.npz', **{f'{k}|{f}': v[i] for k, v in keep_trades.items() for i, f in enumerate(('ms', 'r', 'why', 'side'))})
    base = [r for r in rows if (r['trail'], r['hold'], r['stop'], r['q']) == (1.5, 480, 'widen', 0.0)][0]
    print('\nBASELINE (paper rules):', 'IS', base['IS'], 'OOS', base['OOS'], 'time-exit share', base['time_exits'])
    for k, (a, b) in base['per'].items():
        print(f'   {k:30} IS {a}  OOS {b}')
    def show(title, sel):
        print(f'\n== {title}')
        print(f"  {'trail':>5} {'hold':>5} {'stop':>5} {'q':>4} | {'IS n':>5} {'mean':>7} {'t':>5} | {'OOS n':>5} {'mean':>7} {'t':>5} {'total':>7} | time-exits")
        for r in sel:
            print(f"  {r['trail']:5.1f} {r['hold']:5} {r['stop']:>5} {r['q']:4.1f} | {r['IS']['n']:5} {r['IS']['mean']:+7.3f} {r['IS']['t']:5.2f} | "
                  f"{r['OOS']['n']:5} {r['OOS']['mean']:+7.3f} {r['OOS']['t']:5.2f} {r['OOS']['total']:+7.1f} | {r['time_exits']:.0%}")
    show('Item 2: trail x hold (widen, no spread filter)', [r for r in rows if r['stop'] == 'widen' and r['q'] == 0])
    show('Item 3: skip widened stops (no spread filter)', [r for r in rows if r['stop'] == 'skip' and r['q'] == 0])
    show('Item 4: spread filter (baseline exits)', [r for r in rows if r['trail'] == 1.5 and r['hold'] == 480])
    best = sorted(rows, key=lambda r: -r['IS']['t'])[:8]
    show('Top 8 of all 128 by in-sample t (selection on IS only)', best)


def validate():
    """Compare the baseline replay with the paper trader's own jev trades (data/paper.db) over the overlap."""
    Z = np.load(f'{D}/baseline_trades.npz')
    c = sqlite3.connect('file:/opt/trading-algos/services/deriv-jev/data/paper.db?mode=ro', uri=True)
    P = c.execute("select tag, open_ms, side, r_mult, reason from trades where grp='jev' order by open_ms").fetchall()
    tot = match = 0; dr = []
    for tag, oms, side, r, reason in P:
        ms = Z[f'{tag}|ms']; rr = Z[f'{tag}|r']; sd = Z[f'{tag}|side']
        j = np.searchsorted(ms, oms - 10000); tot += 1
        if j < len(ms) and abs(int(ms[j]) - oms) <= 10000 and np.isfinite(rr[j]) and sd[j] == side:
            match += 1; dr.append(rr[j] - r)
    first = min(p[1] for p in P) if P else 0
    rep = 0
    for tag in {p[0] for p in P}:
        ms = Z[f'{tag}|ms']; rr = Z[f'{tag}|r']; rep += int((np.isfinite(rr) & (ms >= first)).sum())
    dr = np.array(dr)
    print(f'paper jev trades {tot}; matched by replay (same side, entry within 10 s) {match}; replay trades in the same span {rep}')
    if len(dr):
        print(f'R difference replay - paper: median {np.median(dr):+.3f}, mean |diff| {np.abs(dr).mean():.3f}, within 0.1R {np.mean(np.abs(dr) < 0.1):.0%}')


if __name__ == '__main__':
    validate() if len(sys.argv) > 1 and sys.argv[1] == 'validate' else main()
