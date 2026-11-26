"""MT5 runner for the 5 Jev-gated DSI strategies (Deriv MT5 via the RPyC bridge on 127.0.0.1:18812).

Signal and Jev logic are the deriv-jev service's: an ungated engine.Track per strategy is fed MT5 M1 bars
(7-day replay at start, then every minute); each new ungated entry is put to Jev (history-v1 state,
median of JEV_ASKS answers, per-strategy threshold from config.json). An approved signal is traded live:

- Market order, stop = max(stop_atr x ATR, broker minimum + spread + buffer); no take-profit.
- Trailing stop as backtested: once price is 1R in favour, SL trails at best - trail_R x R
  (never closer than the broker minimum). Live, it is applied tick by tick: between the minute steps every tick since
  the last check is read (polled every JEV_TRAIL_POLL seconds, default 1); if a tick crossed a trailing level the broker
  stop had not reached yet, the position is closed at market. The minute step keeps a bar-based trail as a backstop.
- Time exit after max_hold + 1 minutes.
- Size = RISK_PCT of equity / stop distance (compounding). One position per strategy (magic 708001-708005).

Settings in .env.mt5 (MT5_MODE, RISK_PCT, MT5_LOGIN, ...); the Jev key is read from .env (JEV_API_KEY).
MT5_MODE=shadow logs decisions + order_check only. Kill switch: data/KILL_JEV (no new entries).
"""
import json
import logging
import math
import os
import sqlite3
import time
import traceback

import numpy as np
import rpyc

from engine import Track, atr, resample
from jev import ask_median, state_v1
from rules import generic_rules

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data'); os.makedirs(DATA, exist_ok=True)
LOGS = os.path.join(BASE, 'logs'); os.makedirs(LOGS, exist_ok=True)


def load_env(path):
    env = {}
    if os.path.exists(path):
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1); env[k.strip()] = v.strip()
    return env


ENV = load_env(os.path.join(BASE, '.env.mt5'))
JEV_KEY = load_env(os.path.join(BASE, '.env')).get('JEV_API_KEY', '')
MODE = ENV.get('JEV_MT5_MODE', ENV.get('MT5_MODE', 'shadow')).lower()
RISK_PCT = float(ENV.get('JEV_RISK_PCT', ENV.get('RISK_PCT', '5')))
MAX_RISK_PCT = float(ENV.get('MAX_RISK_PCT', '7.5'))
STOP_BUFFER = float(ENV.get('STOP_BUFFER_PCT', '20')) / 100.0
TRAIL_POLL = float(ENV.get('JEV_TRAIL_POLL', '1.0'))      # seconds between tick-level trailing checks (live)
BRIDGE = (ENV.get('BRIDGE_HOST', '127.0.0.1'), int(ENV.get('BRIDGE_PORT', '18812')))
MT5_TERMINAL = ENV.get('MT5_TERMINAL', r'C:\Program Files\MetaTrader 5 Terminal\terminal64.exe')
HIST_MIN = 7 * 1440
SYMBOLS = {'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30'}

CFG = json.load(open(os.path.join(BASE, 'config.json')))
STRATS = [s for s in CFG['strategies'] if s.get('group') == 'jev']
for i, s in enumerate(STRATS):
    s['magic'] = 708001 + i
JEV_ASKS = int(CFG.get('jev_asks', 3)); JEV_MODEL = CFG['jev_model']

logging.basicConfig(filename=os.path.join(LOGS, 'jev_mt5.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('jev_mt5')


def comment(tag):
    p = tag.split('-')            # JEV-DSI30-M1-mom16-q20-trend
    return f"JEV {p[1].replace('DSI', 'D')} {p[2]} {p[3]} {p[4]}"[:31]


class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'jev_mt5.db'), check_same_thread=False)
        self.c.execute('''create table if not exists decisions(id integer primary key, ts real, tag text, side int, signal_t int,
            jev_p real, jev_ps text, threshold real, decision text, error text)''')
        self.c.execute('''create table if not exists orders(id integer primary key, ts real, mode text, tag text, symbol text, side int,
            lots real, price real, sl real, strat_R real, R real, equity real, risk_usd real, retcode int, retmsg text, ticket int, note text)''')
        self.c.execute('''create table if not exists events(id integer primary key, ts real, tag text, ticket int, kind text, detail text)''')
        self.c.commit()

    def ins(self, table, **k):
        cur = self.c.execute(f"insert into {table}({','.join(k)}) values({','.join('?' * len(k))})", tuple(k.values()))
        self.c.commit(); return cur.lastrowid

    def R_of(self, ticket):
        r = self.c.execute('select R from orders where ticket=? order by id desc limit 1', (ticket,)).fetchone()
        return r[0] if r else None


class Runner:
    def __init__(self):
        self.db = DB(); self.conn = None; self.mt5 = None
        sp = {c: CFG['symbols'][c]['spread'] for c in CFG['symbols']}
        self.tracks = {s['tag']: Track(s, sp[s['code']], CFG['swap_per_min']) for s in STRATS}
        self.last_t = {}          # code -> last processed M1 minute
        self.trail = {}           # live ticket -> {'best': exit-side extreme so far, 'ms': last tick checked}
        self.ready = False

    # ---- bridge ----
    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE); self.conn._config['sync_request_timeout'] = 120
        self.mt5 = self.conn.modules.MetaTrader5
        if not self.mt5.initialize(path=MT5_TERMINAL):
            raise RuntimeError(f'initialize failed: {self.mt5.last_error()}')
        a = self.mt5.account_info()
        if ENV.get('MT5_LOGIN') and str(a.login) != ENV['MT5_LOGIN']:
            raise RuntimeError(f'attached to account {a.login}, expected {ENV["MT5_LOGIN"]}')
        for n in SYMBOLS.values():
            self.mt5.symbol_select(n, True)
        log.info('connected: login %s server %s mode %s equity %.2f', a.login, a.server, MODE, a.equity)

    def rcall(self, fn, req):
        plain = {k: (v if isinstance(v, str) else float(v) if isinstance(v, (float, np.floating)) else int(v)) for k, v in req.items()}
        self.conn.execute('import MetaTrader5 as _m5')
        self.conn.execute(f'_req = {plain!r}')
        self.conn.execute(f'_res = _m5.{fn}(_req)')
        return self.conn.eval('_res')

    def m1(self, code, now_min):
        r = rpyc.classic.obtain(self.mt5.copy_rates_from_pos(SYMBOLS[code], self.mt5.TIMEFRAME_M1, 0, HIST_MIN + 600))
        t = (r['time'] // 60).astype(np.int64); m = t < now_min
        return dict(t=t[m], o=r['open'][m].astype(float), h=r['high'][m].astype(float), l=r['low'][m].astype(float), c=r['close'][m].astype(float))

    # ---- signal engine (same as service.process) ----
    def process(self, code, B, idx, live):
        strats = [s for s in STRATS if s['code'] == code]
        cache = {}
        if not live:                                   # replay: one resample per tf over the whole window
            for s in strats:
                tf = s['tf']
                if tf not in cache:
                    b = resample(B['t'], B['o'], B['h'], B['l'], B['c'], tf, int(B['t'][-1]) + 1)
                    a = atr(b); cache[tf] = (b, a, generic_rules(b, a))
        for j in idx:
            t, o, h, l, c = int(B['t'][j]), B['o'][j], B['h'][j], B['l'][j], B['c'][j]
            for s in strats:
                self.tracks[s['tag']].on_m1(t, o, h, l, c)
            for s in strats:
                tf = s['tf']
                if (t + 1) % tf != 0:
                    continue
                if live:
                    if tf not in cache:
                        lo = max(0, j - HIST_MIN)
                        b = resample(B['t'][lo:j + 1], B['o'][lo:j + 1], B['h'][lo:j + 1], B['l'][lo:j + 1], B['c'][lo:j + 1], tf, t + 1)
                        a = atr(b); cache[tf] = (b, a, generic_rules(b, a))
                    b, a, rr = cache[tf]; k = len(b['t']) - 1
                else:
                    b, a, rr = cache[tf]
                    k = int(np.searchsorted(b['t'], t + 1 - tf))
                    if k >= len(b['t']) or b['t'][k] != t + 1 - tf:
                        continue
                if k < 0 or b['t'][k] + tf != t + 1:
                    continue
                P = self.tracks[s['tag']].on_bar(b, a, rr[s['rule']], k)
                if P is not None and live:
                    self.on_signal(s, P, float(s['stop_atr'] * a[k]))
            if live:
                cache = {}

    def on_signal(self, s, P, strat_R):
        st = state_v1(s['code'], P['F'], P['side'], P['hist'], P['moves'], P['signal_t'])
        p, ps, lat, err = ask_median(JEV_KEY, JEV_MODEL, st, JEV_ASKS) if JEV_KEY else (None, [], None, 'JEV_API_KEY missing in .env')
        kill = os.path.exists(os.path.join(DATA, 'KILL_JEV'))
        if p is None:
            dec = 'jev_error'
        elif p < s['jev_threshold']:
            dec = 'skip'
        elif kill:
            dec = 'killed'
        elif self.positions(s):
            dec = 'position_open'
        else:
            dec = 'take'
        self.db.ins('decisions', ts=time.time(), tag=s['tag'], side=P['side'], signal_t=P['signal_t'], jev_p=p, jev_ps=json.dumps(ps),
                    threshold=s['jev_threshold'], decision=dec, error=err)
        log.info('%s signal %s jev_p=%s %s thr=%s -> %s %s', s['tag'], 'LONG' if P['side'] > 0 else 'SHORT', p, ps, s['jev_threshold'], dec, err or '')
        if dec == 'take':
            self.enter(s, P['side'], strat_R)

    # ---- execution ----
    def positions(self, s):
        return [x for x in (self.mt5.positions_get(symbol=SYMBOLS[s['code']]) or []) if x.magic == s['magic']]

    def enter(self, s, side, strat_R):
        mt5 = self.mt5; sym = SYMBOLS[s['code']]
        info = mt5.symbol_info(sym); tick = mt5.symbol_info_tick(sym); acc = mt5.account_info()
        spread = tick.ask - tick.bid; min_d = info.trade_stops_level * info.point
        R = max(strat_R, min_d * (1 + STOP_BUFFER) + spread)
        val = info.trade_tick_value / info.trade_tick_size; equity = float(acc.equity)
        lots = math.floor(equity * RISK_PCT / 100.0 / (R * val) / info.volume_step + 1e-9) * info.volume_step
        lots = min(round(lots, 2), info.volume_max); note = f'R widened {strat_R:.2f}->{R:.2f}' if R > strat_R + 1e-9 else ''
        if lots < info.volume_min:
            risk_min = info.volume_min * R * val
            if equity > 0 and 100 * risk_min / equity <= MAX_RISK_PCT:
                lots = info.volume_min; note += '; min lot'
            else:
                self.db.ins('orders', ts=time.time(), mode=MODE, tag=s['tag'], symbol=sym, side=side, lots=0, price=None, sl=None, strat_R=strat_R,
                            R=R, equity=equity, risk_usd=risk_min, retcode=None, retmsg='skipped', ticket=None, note=f'min lot risks ${risk_min:.2f}')
                log.info('%s SKIP min lot risks $%.2f on $%.2f', s['tag'], risk_min, equity); return
        price = tick.ask if side > 0 else tick.bid
        sl = round(price - side * R, info.digits)
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=sym, volume=float(lots), type=mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL,
                   price=float(price), sl=float(sl), tp=0.0, deviation=200, magic=int(s['magic']), comment=comment(s['tag']),
                   type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else mt5.ORDER_FILLING_IOC)
        r = self.rcall('order_send' if MODE == 'live' else 'order_check', req)
        rc = r.retcode if r else None; msg = r.comment if r else str(mt5.last_error()); ticket = getattr(r, 'order', None) if MODE == 'live' and r else None
        self.db.ins('orders', ts=time.time(), mode=MODE, tag=s['tag'], symbol=sym, side=side, lots=lots, price=price, sl=sl, strat_R=strat_R, R=R,
                    equity=equity, risk_usd=lots * R * val, retcode=rc, retmsg=msg, ticket=ticket, note=note)
        log.info('%s %s %s %.2f lots @ %.2f sl %.2f R %.2f risk $%.2f -> %s %s [%s]', s['tag'], MODE.upper(), 'BUY' if side > 0 else 'SELL',
                 lots, price, sl, R, lots * R * val, rc, msg, note)

    def manage(self, s, B):
        """Trailing stop (after +1R, best - trail_R*R) and time exit for live positions of this strategy."""
        if MODE != 'live':
            return
        mt5 = self.mt5
        for pos in self.positions(s):
            R = self.db.R_of(pos.ticket) or abs(pos.price_open - pos.sl)
            if R <= 0:
                continue
            side = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
            info = mt5.symbol_info(pos.symbol); tick = mt5.symbol_info_tick(pos.symbol); spread = tick.ask - tick.bid
            if time.time() - pos.time >= (int(s['max_hold']) + 1) * 60:
                req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=pos.symbol, volume=float(pos.volume), position=int(pos.ticket),
                           type=mt5.ORDER_TYPE_SELL if side > 0 else mt5.ORDER_TYPE_BUY, price=float(tick.bid if side > 0 else tick.ask),
                           deviation=200, magic=int(pos.magic), comment='time exit', type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_FOK)
                r = self.rcall('order_send', req)
                self.db.ins('events', ts=time.time(), tag=s['tag'], ticket=pos.ticket, kind='time_exit', detail=f'{r.retcode if r else None}')
                log.info('%s time exit %s -> %s', s['tag'], pos.ticket, r.retcode if r else mt5.last_error()); continue
            m = B['t'] >= pos.time // 60
            if side > 0:
                best = max(float(B['h'][m].max()) if m.any() else pos.price_open, tick.bid)
                if best - pos.price_open < R:
                    continue
                new = best - s['trail_R'] * R
                new = min(new, tick.bid - info.trade_stops_level * info.point * (1 + STOP_BUFFER))
                better = new > pos.sl + info.point
            else:
                best = min((float(B['l'][m].min()) + spread) if m.any() else pos.price_open, tick.ask)
                if pos.price_open - best < R:
                    continue
                new = best + s['trail_R'] * R
                new = max(new, tick.ask + info.trade_stops_level * info.point * (1 + STOP_BUFFER))
                better = pos.sl == 0 or new < pos.sl - info.point
            if better:
                new = round(new, info.digits)
                r = self.rcall('order_send', dict(action=mt5.TRADE_ACTION_SLTP, symbol=pos.symbol, position=int(pos.ticket), sl=float(new), tp=0.0, magic=int(pos.magic)))
                self.db.ins('events', ts=time.time(), tag=s['tag'], ticket=pos.ticket, kind='trail', detail=f'{pos.sl}->{new} rc={r.retcode if r else None}')
                log.info('%s trail %s sl %.2f -> %.2f (%s)', s['tag'], pos.ticket, pos.sl, new, r.retcode if r else mt5.last_error())

    def ticks_since(self, sym, ms):
        """Bid/ask ticks strictly after ms (epoch milliseconds), oldest first."""
        self.conn.execute('import MetaTrader5 as _m5')
        self.conn.execute(f'_tk = _m5.copy_ticks_from({sym!r}, {int(ms // 1000)}, 200000, _m5.COPY_TICKS_INFO)\n'
                          '_x = None if _tk is None or len(_tk) == 0 else (_tk["time_msc"].tobytes(), _tk["bid"].tobytes(), _tk["ask"].tobytes())')
        x = self.conn.eval('_x')
        if x is None:
            return np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
        T = np.frombuffer(bytes(x[0]), np.int64); B = np.frombuffer(bytes(x[1]), np.float64); A = np.frombuffer(bytes(x[2]), np.float64)
        m = T > ms
        return T[m], B[m], A[m]

    def close_now(self, s, pos, why):
        mt5 = self.mt5; side = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1; tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=pos.symbol, volume=float(pos.volume), position=int(pos.ticket),
                   type=mt5.ORDER_TYPE_SELL if side > 0 else mt5.ORDER_TYPE_BUY, price=float(tick.bid if side > 0 else tick.ask),
                   deviation=200, magic=int(pos.magic), comment=why[:31], type_time=mt5.ORDER_TIME_GTC,
                   type_filling=mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else mt5.ORDER_FILLING_IOC)
        r = self.rcall('order_send', req)
        self.db.ins('events', ts=time.time(), tag=s['tag'], ticket=pos.ticket, kind='trail_close', detail=f'{why} rc={r.retcode if r else None}')
        log.info('%s %s %s -> %s', s['tag'], why, pos.ticket, r.retcode if r else mt5.last_error())
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE

    def trail_watch(self):
        """Live only: the backtest's trailing stop, tick by tick. Before each tick, the trail level is
        best_so_far - trail_R x R (active once best is 1R in favour); a tick at or through a level the broker stop has
        not reached closes the position at market. Otherwise the broker stop is moved to the latest level."""
        if MODE != 'live':
            return
        mt5 = self.mt5; live = set()
        for s in STRATS:
            if not s.get('trail_R'):
                continue
            for pos in self.positions(s):
                live.add(pos.ticket)
                R = self.db.R_of(pos.ticket) or abs(pos.price_open - pos.sl)
                if R <= 0:
                    continue
                side = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
                st = self.trail.setdefault(pos.ticket, dict(best=pos.price_open, ms=int(pos.time_msc)))
                T, bid, ask = self.ticks_since(pos.symbol, st['ms'])
                if not len(T):
                    continue
                px = bid if side > 0 else ask
                f = side * px                                             # favourable-signed exit price
                prev_best = np.maximum.accumulate(np.r_[side * st['best'], f[:-1]])   # best before each tick
                active = prev_best - side * pos.price_open >= R
                level = prev_best - s['trail_R'] * R                     # favourable-signed trail level before each tick
                broker = side * pos.sl if pos.sl else -np.inf
                hit = np.flatnonzero(active & (level > broker) & (f <= level))
                if len(hit):
                    i = hit[0]
                    why = f'trail tick-level stop at {side * level[i]:.2f} crossed {(int(T[-1]) - int(T[i])) / 1000:.1f}s ago'
                    if self.close_now(s, pos, why):
                        self.trail.pop(pos.ticket, None)
                    continue                                              # on failure keep state; retry next poll
                best = side * max(side * st['best'], f.max())
                st.update(best=best, ms=int(T[-1]))
                if side * (best - pos.price_open) < R:
                    continue
                info = mt5.symbol_info(pos.symbol); tick = mt5.symbol_info_tick(pos.symbol)
                gap = info.trade_stops_level * info.point * (1 + STOP_BUFFER)
                new = best - side * s['trail_R'] * R
                new = min(new, tick.bid - gap) if side > 0 else max(new, tick.ask + gap)
                better = new > pos.sl + info.point if side > 0 else (pos.sl == 0 or new < pos.sl - info.point)
                if not better:
                    continue
                new = round(new, info.digits)
                r = self.rcall('order_send', dict(action=mt5.TRADE_ACTION_SLTP, symbol=pos.symbol, position=int(pos.ticket), sl=float(new),
                                                  tp=0.0, magic=int(pos.magic)))
                self.db.ins('events', ts=time.time(), tag=s['tag'], ticket=pos.ticket, kind='trail_tick',
                            detail=f'{pos.sl}->{new} rc={r.retcode if r else None}')
                log.info('%s trail (tick) %s sl %.2f -> %.2f (%s)', s['tag'], pos.ticket, pos.sl, new, r.retcode if r else mt5.last_error())
        for k in [k for k in self.trail if k not in live]:
            self.trail.pop(k)

    def step(self):
        now = int(time.time() // 60)
        for code in sorted({s['code'] for s in STRATS}):
            B = self.m1(code, now)
            if B is None or len(B['t']) < 2000:
                continue
            if code not in self.last_t:                 # replay history (no Jev calls)
                self.process(code, B, range(len(B['t'])), live=False)
                log.info('%s replayed %d bars', code, len(B['t']))
            else:
                new = np.flatnonzero(B['t'] > self.last_t[code])
                self.process(code, B, new, live=True)
            self.last_t[code] = int(B['t'][-1])
            for s in STRATS:
                if s['code'] == code:
                    self.manage(s, B)

    def run(self):
        log.info('starting jev_mt5: mode=%s risk=%.2f%% strategies=%d key=%s', MODE, RISK_PCT, len(STRATS), 'set' if JEV_KEY else 'MISSING')
        while True:
            try:
                if self.mt5 is None:
                    self.connect()
                self.step()
                wake = time.time() + (63 - time.time() % 60)
                while time.time() < wake - TRAIL_POLL:               # meanwhile: tick-level trailing stop (live)
                    self.trail_watch(); time.sleep(TRAIL_POLL)
            except Exception:
                log.error('loop error: %s', traceback.format_exc())
                try:
                    self.conn and self.conn.close()
                except Exception:
                    pass
                self.conn = self.mt5 = None
                time.sleep(10); continue
            time.sleep(max(0.0, wake - time.time()))


if __name__ == '__main__':
    Runner().run()
