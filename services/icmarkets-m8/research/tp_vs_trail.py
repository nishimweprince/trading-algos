"""Static take profit vs the live exit management (break-even move + reverse on opposite signal), from MFE/MAE.

Part A: every closed LIVE trade of ic_m8 (broker deals via the execution bridge, read-only: no initialize, no orders).
Part B: the same signal engine replayed on the IC Markets tick history (/opt/dsi-analogs/wf_ic/ticks), for a larger sample.

Per trade: entry = real fill (A) or first tick >= bar close + 1.5 s at the ask/bid (B); R = 5 x ATR; break-even after a
4 x ATR (= 0.8R) move; the trade's window ends at the strategy's next signal (where the live runner closes and
reverses). Each variant replays the window tick by tick: stop 1R; optional static take profit k x R; optional break-even;
otherwise exit at the window end. Exits fill at the bid (long) / ask (short) of the tick that triggers them.
The variant "no TP + break-even" is the live -RS logic; its replay is checked against the real results in part A.

Usage (VPS, deriv-jev venv): python tp_vs_trail.py [live|replay|both]
"""
import json, sqlite3, sys, time
import numpy as np

BE_R = 0.8                                   # break-even trigger: 4 ATR / 5 ATR stop
TPS = [None, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
MAGIC = {709001: ('IC-B1000-M5-RS', 'Boom1000', 5, 0), 709002: ('IC-B1000-M5-TP1', 'Boom1000', 5, 1),
         709003: ('IC-B1000-M5-TP2', 'Boom1000', 5, 2), 709004: ('IC-J50-M1-RS', 'Jump50', 1, 0)}
DB = '/opt/trading-algos/services/icmarkets-m8/data/ic_m8.db'
P = dict(lookback=55, min_move=1.5, hold=3, cooldown=30, stop_atr=5.0, be_atr=4.0, atr_n=14)


def sim(side, entry, R, px, ms, tp_k, be, live_tp_k=None):
    """px = exit-side prices (bid for long, ask for short) over the window, ms their times. Returns (exit_px, exit_ms, why)."""
    if len(px) == 0:
        return entry, None, 'empty'
    fav = side * (px - entry)                                       # favourable move in price units
    n = len(px); BIG = n
    i_sl = np.argmax(fav <= -R) if (fav <= -R).any() else BIG
    k = tp_k if tp_k is not None else live_tp_k
    i_tp = np.argmax(fav >= k * R) if (k and (fav >= k * R).any()) else BIG
    i_be = BIG
    if be:
        j = np.argmax(fav >= BE_R * R) if (fav >= BE_R * R).any() else BIG
        if j < BIG and j + 1 < n:
            after = fav[j + 1:] <= 0
            i_be = j + 1 + np.argmax(after) if after.any() else BIG
    i = min(i_sl, i_tp, i_be)
    if i == BIG:
        return px[-1], ms[-1], 'window end'
    why = 'stop' if i == i_sl else ('target' if i == i_tp else 'breakeven')
    return px[i], ms[i], why


def mfe_mae(side, entry, R, px):
    if len(px) == 0:
        return 0.0, 0.0
    fav = side * (px - entry)
    return float(fav.max() / R), float(-fav.min() / R)


def report(title, T, note=''):
    """T: list of dicts with side, R, val (USD per price unit), comm, res{variant: R multiple}, actual_R, mfe, mae."""
    print(f'\n=== {title}: {len(T)} closed trades {note}')
    if not T:
        return
    mfe = np.array([t['mfe'] for t in T]); mae = np.array([t['mae'] for t in T])
    print(f'  MFE (R): median {np.median(mfe):.2f}, p25 {np.percentile(mfe, 25):.2f}, p75 {np.percentile(mfe, 75):.2f} | '
          f'MAE (R): median {np.median(mae):.2f}, p75 {np.percentile(mae, 75):.2f}')
    print('  share of trades whose MFE reached: ' + ', '.join(f'{k}R {np.mean(mfe >= k):.0%}' for k in (0.25, 0.5, 0.8, 1, 1.5, 2, 3)))
    if 'actual_R' in T[0]:
        a = np.array([t['actual_R'] for t in T]); usd = sum(t['actual_usd'] for t in T)
        print(f'  ACTUAL (as traded): mean {a.mean():+.3f}R, total {a.sum():+.2f}R, win {np.mean(a > 0):.0%}, net ${usd:+.2f}')
    rows = []
    for v in T[0]['res']:
        r = np.array([t['res'][v] for t in T]); usd = sum(t['res'][v] * t['R'] * t['val'] - t['comm'] for t in T)
        half = len(r) // 2
        rows.append((v, r.mean(), r.sum(), np.mean(r > 0), usd, r[:half].mean() if half else 0, r[half:].mean() if half else 0,
                     r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else 0))
    print(f"  {'variant':22} {'mean R':>8} {'± se':>6} {'total R':>8} {'win':>5} {'net $':>9} {'1st half':>9} {'2nd half':>9}")
    for v, m, s, w, usd, h1, h2, se in rows:
        print(f'  {v:22} {m:+8.3f} {se:6.3f} {s:+8.2f} {w:5.0%} {usd:+9.2f} {h1:+9.3f} {h2:+9.3f}')


def variants(side, entry, R, px, ms, live_tp_k=None):
    out = {}
    for be in (True, False):
        for k in TPS:
            name = ('no TP' if k is None else f'TP {k:g}R') + (' + BE' if be else '')
            xp, _, _ = sim(side, entry, R, px, ms, k, be)
            out[name] = side * (xp - entry) / R
    return out


# ---------------- part A: live trades ----------------
def live():
    import rpyc
    conn = rpyc.classic.connect('127.0.0.1', 18814); conn._config['sync_request_timeout'] = 120
    mt5 = conn.modules.MetaTrader5                                  # the runner owns the session: no initialize/shutdown
    conn.execute('import MetaTrader5 as _m5')
    tick = mt5.symbol_info_tick('Jump50'); off = int(round((tick.time_msc / 1000.0 - time.time()) / 3600.0) * 3600)
    srv_now = int(time.time()) + off
    deals = mt5.history_deals_get(srv_now - 30 * 86400, srv_now + 60)
    D = rpyc.classic.obtain(conn.eval('lambda rows: [dict(r._asdict()) for r in rows]')(deals))
    info = {}
    for s in ('Boom1000', 'Jump50'):
        i = mt5.symbol_info(s); info[s] = i.trade_tick_value / i.trade_tick_size
    pos = {}
    for d in D:
        if d['type'] not in (0, 1) or not d['position_id']:
            continue
        p = pos.setdefault(d['position_id'], dict(open=None, closes=[]))
        if d['entry'] == 0:
            p['open'] = d
        else:
            p['closes'].append(d)
    db = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    trades = []
    for pid, p in pos.items():
        o = p['open']
        if o is None or o['magic'] not in MAGIC or not p['closes']:
            continue
        tag, sym, tf, mode = MAGIC[o['magic']]
        row = db.execute("select R, atr from orders where tag=? and ticket=? and kind='open' order by id desc limit 1", (tag, pid)).fetchone()
        if not row or not row[0]:
            print('  no R for', tag, pid); continue
        trades.append(dict(pid=pid, tag=tag, sym=sym, mode=mode, side=1 if o['type'] == 0 else -1, entry=o['price'], open_ms=o['time_msc'],
                           vol=o['volume'], R=row[0], close_ms=max(c['time_msc'] for c in p['closes']),
                           exit=p['closes'][-1]['price'], usd=sum(c['profit'] + c['swap'] + c['commission'] + c['fee'] for c in p['closes']) + o['commission'] + o['fee'],
                           comm=-(o['commission'] + o['fee'] + sum(c['commission'] + c['fee'] for c in p['closes'])),
                           reason=p['closes'][-1]['reason']))
    opens = sorted(((d['magic'], d['time_msc']) for d in D if d['entry'] == 0 and d['magic'] in MAGIC), key=lambda x: x[1])
    out = {}
    for t in sorted(trades, key=lambda t: t['open_ms']):
        nxt = [ms for m, ms in opens if m == MAGIC_BY_TAG[t['tag']] and ms > t['open_ms']]
        end_ms = nxt[0] if nxt else (srv_now * 1000)
        conn.execute(f"_tk = _m5.copy_ticks_range({t['sym']!r}, {t['open_ms'] // 1000 - 1}, {end_ms // 1000 + 2}, _m5.COPY_TICKS_ALL)\n"
                     "_x = None if _tk is None or len(_tk) == 0 else (_tk['time_msc'].tobytes(), _tk['bid'].tobytes(), _tk['ask'].tobytes())")
        x = conn.eval('_x')
        ms = np.frombuffer(bytes(x[0]), np.int64); bid = np.frombuffer(bytes(x[1]), np.float64); ask = np.frombuffer(bytes(x[2]), np.float64)
        w = (ms > t['open_ms']) & (ms <= end_ms); a = (ms > t['open_ms']) & (ms <= t['close_ms'])
        px = (bid if t['side'] > 0 else ask)
        mfe, mae = mfe_mae(t['side'], t['entry'], t['R'], px[a])
        res = variants(t['side'], t['entry'], t['R'], px[w], ms[w])
        # the live strategy itself, replayed: TP at mode x R (0 = none) + break-even
        xp, xms, why = sim(t['side'], t['entry'], t['R'], px[w], ms[w], None, True, live_tp_k=t['mode'] or None)
        actual_R = t['side'] * (t['exit'] - t['entry']) / t['R']
        out.setdefault(t['tag'], []).append(dict(side=t['side'], R=t['R'], val=info[t['sym']] * t['vol'], comm=t['comm'], res=res,
                                                 actual_R=actual_R, actual_usd=t['usd'], mfe=mfe, mae=mae,
                                                 replay_R=t['side'] * (xp - t['entry']) / t['R'], replay_why=why, reason=t['reason']))
    conn.close()
    for tag, T in out.items():
        report(f'LIVE {tag}', T, '(broker fills; window = until the next signal)')
        d = np.array([t['replay_R'] - t['actual_R'] for t in T])
        print(f'  replay check (live logic re-simulated vs broker result): mean |diff| {np.abs(d).mean():.3f}R, max {np.abs(d).max():.3f}R')


MAGIC_BY_TAG = {v[0]: k for k, v in MAGIC.items()}


# ---------------- part B: replay on tick history ----------------
def wilder_atr(h, l, c, n):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    a = np.full(len(c), np.nan)
    if len(c) < n:
        return a
    a[n - 1] = tr[:n].mean()
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


def replay():
    for sym, tf, tags in (('Boom1000', 5, 'IC-B1000-M5 (signals shared by RS/TP1/TP2)'), ('Jump50', 1, 'IC-J50-M1')):
        z = np.load(f'/opt/dsi-analogs/wf_ic/ticks/{sym}.npz'); ms, bid, ask = z['ms'], z['bid'], z['ask']
        ok = (bid > 0) & (ask > 0) & (ask >= bid); ms, bid, ask = ms[ok], bid[ok], ask[ok]
        o = np.argsort(ms, kind='stable'); ms, bid, ask = ms[o], bid[o], ask[o]
        mins = ms // 60000; st = np.flatnonzero(np.r_[True, mins[1:] != mins[:-1]]); en = np.r_[st[1:], len(ms)]
        t1 = mins[st]; o1 = bid[st]; c1 = bid[en - 1]; h1 = np.maximum.reduceat(bid, st); l1 = np.minimum.reduceat(bid, st)
        g = t1 // tf; s2 = np.flatnonzero(np.r_[True, g[1:] != g[:-1]]); e2 = np.r_[s2[1:], len(t1)]
        bt = g[s2] * tf; c = c1[e2 - 1]; h = np.maximum.reduceat(h1, s2); l = np.minimum.reduceat(l1, s2)
        a = wilder_atr(h, l, c, P['atr_n']); sig = signals(c, a, P['lookback'], P['min_move'], P['hold'], P['cooldown'])
        k = np.flatnonzero(sig != 0)
        ent_ms = (bt[k] + tf) * 60000 + 1500                                # runner acts ~1 s after the bar closes
        ei = np.searchsorted(ms, ent_ms)
        T = []
        for j in range(len(k)):
            if ei[j] >= len(ms):
                break
            side = int(sig[k[j]]); R = P['stop_atr'] * a[k[j]]; e = ei[j]
            end = ei[j + 1] if j + 1 < len(k) else len(ms)
            entry = ask[e] if side > 0 else bid[e]
            px = (bid if side > 0 else ask)[e + 1:end + 1]; tt = ms[e + 1:end + 1]
            mfe, mae = mfe_mae(side, entry, R, px)
            T.append(dict(side=side, R=R, val=0.0, comm=0.0, res=variants(side, entry, R, px, tt), mfe=mfe, mae=mae))
        days = (ms[-1] - ms[0]) / 864e5
        report(f'REPLAY {sym} M{tf} - {tags}', T, f'over {days:.0f} days of IC ticks (R multiples; $ not applicable)')


if __name__ == '__main__':
    what = sys.argv[1] if len(sys.argv) > 1 else 'both'
    if what in ('live', 'both'):
        live()
    if what in ('replay', 'both'):
        replay()
