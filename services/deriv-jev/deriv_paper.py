"""Deriv paper trader: all DSI strategies from config.json (groups wr70 and jev, Jev gate removed), simulated on the
live Deriv MT5 quote stream. Nothing is sent to the broker.

Signals: exactly as the live runners (engine.resample + engine.atr + rules.generic_rules on completed tf bars).
Fills:   every entry and exit at a real Deriv MT5 tick, buys at the ask and sells at the bid, so each trade pays
         the spread actually quoted at that moment.
Stops:   strategy stop (stop_atr x ATR) widened to the broker minimum (stops_level x 1.2 + spread), as live orders
         would be. wr70: fixed target (target_R x strategy stop, at least the broker minimum). jev group: no target,
         trailing stop at best - trail_R x R once 1R in profit. Both: time exit after max_hold + 1 minutes.
         Stops/targets/trails are checked on every tick in order.
Sizing:  one paper account per group (PAPER_EQUITY, default $100), PAPER_RISK_PCT (default 5) of current equity per
         trade, compounding; minimum lot allowed up to PAPER_MAX_RISK_PCT (7.5), otherwise the trade is skipped.
State is kept in data/paper.db (survives restarts). Reset = stop the service and delete data/paper.db.
"""
import json, logging, math, os, sqlite3, sys, time, traceback
import numpy as np
import rpyc

BASE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, BASE)
from engine import atr, resample
from rules import generic_rules

DATA = os.path.join(BASE, 'data'); LOGS = os.path.join(BASE, 'logs')
os.makedirs(DATA, exist_ok=True); os.makedirs(LOGS, exist_ok=True)


def load_env(path):
    env = {}
    if os.path.exists(path):
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1); env[k.strip()] = v.strip()
    return env


ENV = load_env(os.path.join(BASE, '.env.mt5'))
BRIDGE = (ENV.get('BRIDGE_HOST', '127.0.0.1'), int(ENV.get('BRIDGE_PORT', '18812')))
EQ0 = float(ENV.get('PAPER_EQUITY', '100'))
RISK = float(ENV.get('PAPER_RISK_PCT', '5'))
MAXRISK = float(ENV.get('PAPER_MAX_RISK_PCT', '7.5'))
BUF = float(ENV.get('STOP_BUFFER_PCT', '20')) / 100.0
SYMBOLS = {'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30'}
import paper_cfg
STRATS, GLABELS, _, RETIRED = paper_cfg.load()           # wr70 + jev from config.json, plus paper_variants.json accounts
GROUPS = sorted({s['group'] for s in STRATS})
ACTIVE = [s for s in STRATS if s['group'] not in RETIRED]     # retired groups: no new entries, open positions still managed
HIST = 6000                                              # M1 bars: >= 300 completed M15 bars + ATR warm-up

logging.basicConfig(filename=os.path.join(LOGS, 'deriv_paper.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('paper')


class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'paper.db'), isolation_level=None)
        self.c.execute('''create table if not exists pos(tag text primary key, grp text, code text, side int, lots real, entry real,
            sl real, tp real, R real, trail_R real, best real, open_ms int, last_ms int, max_hold int, entry_spread real, note text)''')
        self.c.execute('''create table if not exists trades(id integer primary key, tag text, grp text, code text, side int, lots real,
            entry real, exit real, R real, r_mult real, pnl real, open_ms int, close_ms int, reason text, entry_spread real,
            exit_spread real, equity_after real, note text)''')
        self.c.execute('''create table if not exists skips(id integer primary key, ts real, tag text, side int, note text)''')
        self.c.execute('create table if not exists state(k text primary key, v text)')
        if self.get('started') is None:
            self.put('started', time.time())
        for g in GROUPS:                                     # accounts added later start at EQ0 when first seen
            if self.get(f'equity:{g}') is None:
                self.put(f'equity:{g}', EQ0); self.put(f'started:{g}', time.time())

    def get(self, k, d=None):
        r = self.c.execute('select v from state where k=?', (k,)).fetchone(); return r[0] if r else d

    def put(self, k, v):
        self.c.execute('insert or replace into state values(?,?)', (k, str(v)))


class Paper:
    def __init__(self):
        self.db = DB(); self.conn = None; self.mt5 = None

    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE); self.conn._config['sync_request_timeout'] = 60
        self.conn.execute('import MetaTrader5 as _m5'); self.mt5 = self.conn.modules.MetaTrader5
        # attach to the Deriv terminal ourselves (needed after a terminal restart; harmless if already attached)
        self.conn.namespace['_term'] = ENV.get('MT5_TERMINAL', r'C:\Program Files\MetaTrader 5 Terminal\terminal64.exe')
        self.conn.execute('_ok = _m5.terminal_info() is not None or _m5.initialize(path=_term, timeout=60000)')
        if not self.conn.eval('_ok'):
            raise RuntimeError(f"MT5 initialize failed: {self.conn.eval('str(_m5.last_error())')}")
        for n in SYMBOLS.values():
            self.mt5.symbol_select(n, True)
        self.info = {}
        for c, n in SYMBOLS.items():
            i = self.mt5.symbol_info(n)
            if i is None:
                raise RuntimeError(f'{n}: no symbol info yet (terminal still loading?)')
            self.info[c] = dict(min_d=i.trade_stops_level * i.point, val=i.trade_tick_value / i.trade_tick_size,
                                step=i.volume_step, vmin=i.volume_min, vmax=i.volume_max, digits=i.digits)
        log.info('connected (read-only quotes); active groups %s, retired %s, $%.0f each, %g%% risk',
                 sorted({s['group'] for s in ACTIVE}), sorted(RETIRED), EQ0, RISK)

    def m1(self, code, now_min):
        self.conn.execute(f'_r = _m5.copy_rates_from_pos({SYMBOLS[code]!r}, _m5.TIMEFRAME_M1, 0, {HIST})')
        r = rpyc.classic.obtain(self.conn.eval('_r'))
        if r is None or len(r) == 0:
            return None
        t = (r['time'] // 60).astype(np.int64); m = t < now_min
        return dict(t=t[m], o=r['open'][m].astype(float), h=r['high'][m].astype(float), l=r['low'][m].astype(float), c=r['close'][m].astype(float))

    def ticks(self, code, ms):
        self.conn.execute(f'_tk = _m5.copy_ticks_from({SYMBOLS[code]!r}, {int(ms // 1000)}, 100000, _m5.COPY_TICKS_INFO)')
        r = rpyc.classic.obtain(self.conn.eval('_tk'))
        if r is None or len(r) == 0:
            return np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
        k = r['time_msc'] > ms
        return r['time_msc'][k].astype(np.int64), r['bid'][k].astype(float), r['ask'][k].astype(float)

    def spread_ref(self, code):
        """Median of the 1-minute mean spreads over the 60 minutes before the current minute (cached per minute)."""
        now_min = int(time.time() // 60); key = (code, now_min)
        if getattr(self, '_sref', (None, None))[0] == key:
            return self._sref[1]
        T, B, A = self.ticks(code, (now_min - 60) * 60000 - 1)
        m = T < now_min * 60000; T, B, A = T[m], B[m], A[m]
        ref = None
        if len(T):
            mins = T // 60000; u, inv = np.unique(mins, return_inverse=True)
            ref = float(np.median(np.bincount(inv, A - B) / np.bincount(inv)))
        self._sref = (key, ref)
        return ref

    def equity(self, g):
        return float(self.db.get(f'equity:{g}', EQ0))

    # ---- entries ----
    def enter(self, s, side, stop_d):
        code = s['code']; I = self.info[code]; tk = self.mt5.symbol_info_tick(SYMBOLS[code])
        if tk is None or tk.bid <= 0 or time.time() - tk.time_msc / 1000.0 > 30:
            self.db.c.execute('insert into skips(ts, tag, side, note) values(?,?,?,?)', (time.time(), s['tag'], side, 'no fresh quote')); return
        spread = tk.ask - tk.bid
        sl_d = max(stop_d, I['min_d'] * (1 + BUF) + spread); note = []
        if s.get('stop_mode') == 'skip' and sl_d > stop_d + 1e-9:          # natural stops only
            self.db.c.execute('insert into skips(ts, tag, side, note) values(?,?,?,?)',
                              (time.time(), s['tag'], side, f'natural stop {stop_d:.2f} below broker minimum {sl_d:.2f}')); return
        q = float(s.get('spread_min_rel', 0) or 0)
        if q > 0:                                                            # skip narrow spreads
            ref = self.spread_ref(code)
            if ref and spread / ref < q:
                self.db.c.execute('insert into skips(ts, tag, side, note) values(?,?,?,?)',
                                  (time.time(), s['tag'], side, f'spread {spread:.2f} < {q:g} x prior-hour median {ref:.2f}')); return
            note.append(f'spread {spread / ref:.2f}x median' if ref else 'no spread reference')
        if sl_d > stop_d + 1e-9:
            note.append(f'stop widened {stop_d:.2f}->{sl_d:.2f}')
        tpR = float(s.get('target_R', 0) or 0)
        tp_d = max(tpR * stop_d, I['min_d'] * (1 + BUF)) if tpR > 0 else 0.0
        eq = self.equity(s['group']); val = I['val']
        lots = math.floor(eq * RISK / 100.0 / (sl_d * val) / I['step'] + 1e-9) * I['step']
        if lots < I['vmin']:
            if I['vmin'] * sl_d * val <= eq * MAXRISK / 100.0:
                lots = I['vmin']; note.append('min lot')
            else:
                self.db.c.execute('insert into skips(ts, tag, side, note) values(?,?,?,?)',
                                  (time.time(), s['tag'], side, f'min lot risks ${I["vmin"] * sl_d * val:.2f} on ${eq:.2f}'))
                log.info('%s SKIP min lot risks $%.2f on $%.2f', s['tag'], I['vmin'] * sl_d * val, eq); return
        lots = round(min(lots, I['vmax']), 2)
        px = tk.ask if side > 0 else tk.bid
        sl = px - side * sl_d; tp = px + side * tp_d if tp_d > 0 else 0.0
        self.db.c.execute('insert or replace into pos values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (s['tag'], s['group'], code, side, lots, px, sl, tp, sl_d, float(s.get('trail_R', 0) or 0), px,
                           int(tk.time_msc), int(tk.time_msc), int(s['max_hold']), spread, '; '.join(note)))
        log.info('%s OPEN %s %.2f @ %.3f sl %.3f tp %s spread %.3f risk $%.2f %s', s['tag'], 'BUY' if side > 0 else 'SELL', lots, px, sl,
                 f'{tp:.3f}' if tp else '-', spread, lots * sl_d * val, '; '.join(note))

    # ---- management, tick by tick ----
    def close(self, p, px, ms, spr, why):
        tag, grp, code, side, lots, e, R = p['tag'], p['grp'], p['code'], p['side'], p['lots'], p['entry'], p['R']
        pnl = side * (px - e) * lots * self.info[code]['val']
        eq = self.equity(grp) + pnl; self.db.put(f'equity:{grp}', round(eq, 4))
        self.db.c.execute('''insert into trades(tag, grp, code, side, lots, entry, exit, R, r_mult, pnl, open_ms, close_ms, reason,
            entry_spread, exit_spread, equity_after, note) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (tag, grp, code, side, lots, e, px, R, side * (px - e) / R, pnl, p['open_ms'], ms, why, p['entry_spread'], spr, eq, p['note']))
        self.db.c.execute('delete from pos where tag=?', (tag,))
        log.info('%s CLOSE %s @ %.3f (%s) %+.2fR $%+.2f -> %s equity $%.2f', tag, 'long' if side > 0 else 'short', px, why,
                 side * (px - e) / R, pnl, grp, eq)

    def manage(self, code):
        cols = [d[1] for d in self.db.c.execute('pragma table_info(pos)')]
        for row in self.db.c.execute('select * from pos where code=?', (code,)).fetchall():
            p = dict(zip(cols, row)); side = p['side']
            T, B, A = self.ticks(code, p['last_ms'])
            sl, tp, best, R, tr = p['sl'], p['tp'], p['best'], p['R'], p['trail_R']
            deadline = p['open_ms'] + (p['max_hold'] + 1) * 60000
            done = False
            for k in range(len(T)):
                px = B[k] if side > 0 else A[k]                       # long exits at bid, short at ask
                spr = A[k] - B[k]
                if (side > 0 and px <= sl) or (side < 0 and px >= sl):
                    self.close(p, px, int(T[k]), spr, 'stop' if side * (sl - p['entry']) < 0 else 'trail'); done = True; break
                if tp and side * (px - tp) >= 0:
                    self.close(p, px, int(T[k]), spr, 'target'); done = True; break
                if T[k] >= deadline:
                    self.close(p, px, int(T[k]), spr, 'time'); done = True; break
                if tr > 0:
                    best = max(best, px) if side > 0 else min(best, px)
                    if side * (best - p['entry']) >= R:
                        nsl = best - side * tr * R
                        sl = max(sl, nsl) if side > 0 else min(sl, nsl)
            if not done and len(T):
                self.db.c.execute('update pos set sl=?, best=?, last_ms=? where tag=?', (sl, best, int(T[-1]), p['tag']))

    # ---- loop ----
    def step(self):
        now = int(time.time() // 60)
        for code in SYMBOLS:
            self.manage(code)
            B = self.m1(code, now)
            if B is None or len(B['t']) < 1000:
                continue
            cache = {}
            for s in [x for x in ACTIVE if x['code'] == code]:
                tf = s['tf']
                if tf not in cache:
                    bb = resample(B['t'], B['o'], B['h'], B['l'], B['c'], tf, now); cache[tf] = [bb, None, None]
                b = cache[tf][0]
                k = len(b['t']) - 1
                if k < 300:
                    continue
                bar_t = int(b['t'][k]); key = f"last_bar:{s['tag']}"
                if self.db.get(key) is None:
                    self.db.put(key, bar_t); continue                  # never act on a bar from before start-up
                if bar_t <= int(self.db.get(key)):
                    continue
                self.db.put(key, bar_t)
                if now - (bar_t + tf) > 2:                              # stale (service was down): skip, do not chase
                    continue
                if cache[tf][1] is None:
                    cache[tf][1] = atr(b); cache[tf][2] = generic_rules(b, cache[tf][1])
                a = cache[tf][1]; sig = cache[tf][2][s['rule']]
                side = int(sig[k])
                if side == 0 or not np.isfinite(a[k]) or a[k] <= 0:
                    continue
                if self.db.c.execute('select 1 from pos where tag=?', (s['tag'],)).fetchone():
                    continue                                            # one position per strategy
                self.enter(s, side, float(s['stop_atr'] * a[k]))

    def run(self):
        while True:
            try:
                if self.conn is None:
                    self.connect()
                self.step()
            except Exception:
                log.error('loop error\n%s', traceback.format_exc()); self.conn = None; time.sleep(15)
            time.sleep(max(1.0, 61 - time.time() % 60))


if __name__ == '__main__':
    Paper().run()
