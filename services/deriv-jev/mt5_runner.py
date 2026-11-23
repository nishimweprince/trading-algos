"""MT5 runner for the 70%+ win-rate DSI group (Deriv MT5, via the RPyC bridge on 127.0.0.1:18812).

Signals are computed exactly as in the backtest / deriv-jev service (engine.resample + engine.atr +
rules.generic_rules on completed tf bars). Execution differences, all deliberate and logged:

- Stop and target distances are widened to the broker minimum (trade_stops_level + spread + buffer)
  when the strategy's own distance is smaller; otherwise MT5 rejects the order.
- Size = RISK_PCT of current equity / effective stop distance (compounding). Rounded down to the
  volume step; a trade whose minimum lot would risk more than MAX_RISK_PCT is skipped.
- One position per strategy (magic number per strategy), hedging account.
- Time exit after max_hold + 1 minutes, as in the backtest.

MT5_MODE (in .env.mt5):
  shadow  (default) build every order, validate it with order_check, and track a virtual position
          on a virtual account (SHADOW_EQUITY, default $100, compounding); nothing is sent.
  live    send orders with order_send. Set this yourself; the service never switches itself.
Kill switch: touch data/KILL_MT5 (no new entries; open positions keep their SL/TP).
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

from engine import atr, resample
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
MODE = ENV.get('MT5_MODE', 'shadow').lower()
RISK_PCT = float(ENV.get('RISK_PCT', '5'))
MAX_RISK_PCT = float(ENV.get('MAX_RISK_PCT', '7.5'))
BRIDGE = (ENV.get('BRIDGE_HOST', '127.0.0.1'), int(ENV.get('BRIDGE_PORT', '18812')))
SHADOW_EQUITY = float(ENV.get('SHADOW_EQUITY', '100'))
STOP_BUFFER = float(ENV.get('STOP_BUFFER_PCT', '20')) / 100.0   # extra margin over the broker minimum
# Explicit terminal path: with several MT5 terminals on the host, initialize() without a path may attach to the wrong one.
MT5_TERMINAL = ENV.get('MT5_TERMINAL', r'C:\Program Files\MetaTrader 5 Terminal\terminal64.exe')
HIST_MIN = 7 * 1440
MAGIC_BASE = 707000
SYMBOLS = {'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30'}

CFG = json.load(open(os.path.join(BASE, 'config.json')))
STRATS = [s for s in CFG['strategies'] if s.get('group') == 'wr70']
for i, s in enumerate(STRATS):
    s['magic'] = MAGIC_BASE + i + 1

logging.basicConfig(filename=os.path.join(LOGS, 'mt5_runner.log'), level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('mt5')


def short_comment(tag):
    # e.g. WR70-DSI10-M5-mom4-q10-trend-1atr-0.5R -> W70 D10 M5 mom4 1a .5R (MT5 comment <= 31 chars)
    p = tag.split('-')
    return f"W70 {p[1].replace('DSI', 'D')} {p[2]} {p[3]} {p[-2].replace('atr', 'a')} {p[-1]}"[:31]


class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'mt5.db'), check_same_thread=False)
        self.c.execute('''create table if not exists orders(id integer primary key, ts real, mode text, tag text, symbol text,
            side int, signal_t int, lots real, price real, sl real, tp real, strat_sl_d real, sl_d real, tp_d real,
            equity real, risk_usd real, risk_pct real, retcode int, retmsg text, ticket int, note text)''')
        self.c.execute('''create table if not exists exits(id integer primary key, ts real, mode text, tag text, ticket int,
            reason text, price real, pnl real, order_id int)''')
        self.c.commit()

    def order(self, **k):
        cols = ','.join(k); q = ','.join('?' * len(k))
        cur = self.c.execute(f'insert into orders({cols}) values({q})', tuple(k.values())); self.c.commit()
        return cur.lastrowid

    def shadow_pnl(self):
        return float(self.c.execute("select coalesce(sum(pnl), 0) from exits where mode != 'live'").fetchone()[0])

    def exit(self, **k):
        cols = ','.join(k); q = ','.join('?' * len(k))
        self.c.execute(f'insert into exits({cols}) values({q})', tuple(k.values())); self.c.commit()


class Runner:
    def __init__(self):
        self.db = DB()
        self.conn = None; self.mt5 = None
        self.last_bar = {}      # tag -> last evaluated tf bar start (minute)
        self.virtual = {}       # shadow mode: tag -> position dict
        self.started_min = int(time.time() // 60)

    # ---- bridge ----
    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE)
        self.conn._config['sync_request_timeout'] = 120
        self.mt5 = self.conn.modules.MetaTrader5
        if not self.mt5.initialize(path=MT5_TERMINAL):
            raise RuntimeError(f'initialize failed: {self.mt5.last_error()}')
        for n in SYMBOLS.values():
            self.mt5.symbol_select(n, True)
        a = self.mt5.account_info()
        if ENV.get('MT5_LOGIN') and str(a.login) != ENV['MT5_LOGIN']:
            raise RuntimeError(f'attached to account {a.login}, expected {ENV["MT5_LOGIN"]}')
        log.info('connected: login %s server %s mode %s equity %.2f %s', a.login, a.server, MODE, a.equity, a.currency)

    def rcall(self, fn, req):
        # Build the request as a plain dict on the Windows side and call MT5 there with one positional
        # argument: MetaTrader5 5.0.62xx rejects the RPyC-proxied call ("Unnamed arguments not allowed").
        plain = {k: (v if isinstance(v, str) else float(v) if isinstance(v, (float, np.floating)) else int(v))
                 for k, v in req.items()}
        self.conn.execute('import MetaTrader5 as _m5')
        self.conn.execute(f'_req = {plain!r}')
        self.conn.execute(f'_res = _m5.{fn}(_req)')      # a statement: the eval() path returns None here
        return self.conn.eval('_res')

    def m1(self, code, now_min):
        r = rpyc.classic.obtain(self.mt5.copy_rates_from_pos(SYMBOLS[code], self.mt5.TIMEFRAME_M1, 0, HIST_MIN))
        if r is None or len(r) == 0:
            return None
        t = (r['time'] // 60).astype(np.int64)
        m = t < now_min                                     # completed M1 bars only
        return dict(t=t[m], o=r['open'][m].astype(float), h=r['high'][m].astype(float),
                    l=r['low'][m].astype(float), c=r['close'][m].astype(float))

    # ---- sizing / stops ----
    def plan(self, s, side, stop_d):
        mt5 = self.mt5; sym = SYMBOLS[s['code']]
        info = mt5.symbol_info(sym); tick = mt5.symbol_info_tick(sym); acc = mt5.account_info()
        spread = tick.ask - tick.bid
        min_d = info.trade_stops_level * info.point
        floor_d = min_d * (1 + STOP_BUFFER) + spread                  # SL is checked against the opposite price
        sl_d = max(stop_d, floor_d)
        tp_d = max(float(s['target_R']) * stop_d, min_d * (1 + STOP_BUFFER))
        val = info.trade_tick_value / info.trade_tick_size              # $ per 1.0 price per lot
        equity = float(acc.equity)
        if MODE != 'live':                      # shadow: virtual account starting at SHADOW_EQUITY, compounding
            equity = SHADOW_EQUITY + self.db.shadow_pnl()
        risk_usd = equity * RISK_PCT / 100.0
        step = info.volume_step
        lots = math.floor(risk_usd / (sl_d * val) / step + 1e-9) * step
        lots = min(round(lots, 8), info.volume_max)
        note = []
        if sl_d > stop_d + 1e-9:
            note.append(f'sl widened {stop_d:.2f}->{sl_d:.2f}')
        if tp_d > s['target_R'] * stop_d + 1e-9:
            note.append(f"tp widened {s['target_R'] * stop_d:.2f}->{tp_d:.2f}")
        if lots < info.volume_min:
            lots_min_risk = info.volume_min * sl_d * val
            if equity > 0 and 100 * lots_min_risk / equity <= MAX_RISK_PCT:
                lots = info.volume_min; note.append('min lot')
            else:
                lots = 0.0; note.append(f'skip: min lot risks ${lots_min_risk:.2f} on equity ${equity:.2f}')
        price = tick.ask if side > 0 else tick.bid
        sl = round(price - side * sl_d, info.digits); tp = round(price + side * tp_d, info.digits)
        real_risk = lots * sl_d * val
        return dict(sym=sym, price=price, sl=sl, tp=tp, sl_d=sl_d, tp_d=tp_d, lots=round(lots, 2), equity=equity,
                    risk_usd=real_risk, risk_pct=(100 * real_risk / equity) if equity > 0 else None,
                    digits=info.digits, filling=info.filling_mode, note='; '.join(note))

    def request(self, s, side, p):
        mt5 = self.mt5
        fill = mt5.ORDER_FILLING_FOK if p['filling'] & 1 else (mt5.ORDER_FILLING_IOC if p['filling'] & 2 else mt5.ORDER_FILLING_RETURN)
        return dict(action=mt5.TRADE_ACTION_DEAL, symbol=p['sym'], volume=float(p['lots']),
                    type=mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL, price=float(p['price']),
                    sl=float(p['sl']), tp=float(p['tp']), deviation=200, magic=int(s['magic']),
                    comment=short_comment(s['tag']), type_time=mt5.ORDER_TIME_GTC, type_filling=fill)

    def open_positions(self, s):
        ps = self.mt5.positions_get(symbol=SYMBOLS[s['code']]) or []
        return [p for p in ps if p.magic == s['magic']]

    # ---- per minute ----
    def step(self):
        now_min = int(time.time() // 60)
        kill = os.path.exists(os.path.join(DATA, 'KILL_MT5'))
        bars = {}
        for s in STRATS:
            code = s['code']
            if code not in bars:
                bars[code] = self.m1(code, now_min)
            B = bars[code]
            if B is None or len(B['t']) < 1000:
                continue
            self.manage(s, B, now_min)
            tf = s['tf']
            if now_min % tf != 0:
                continue
            b = resample(B['t'], B['o'], B['h'], B['l'], B['c'], tf, now_min)
            k = len(b['t']) - 1
            if k < 300 or b['t'][k] + tf != now_min or self.last_bar.get(s['tag']) == int(b['t'][k]):
                continue
            self.last_bar[s['tag']] = int(b['t'][k])
            a = atr(b); side = int(generic_rules(b, a)[s['rule']][k])
            if side == 0 or not np.isfinite(a[k]) or a[k] <= 0:
                continue
            busy = (s['tag'] in self.virtual) if MODE != 'live' else bool(self.open_positions(s))
            if busy:
                log.info('%s signal %s ignored: position open', s['tag'], side)
                continue
            if kill:
                log.info('%s signal %s ignored: KILL_MT5', s['tag'], side); continue
            self.enter(s, side, float(s['stop_atr'] * a[k]), now_min)

    def enter(self, s, side, stop_d, now_min):
        p = self.plan(s, side, stop_d)
        base = dict(ts=time.time(), mode=MODE, tag=s['tag'], symbol=p['sym'], side=side, signal_t=now_min, lots=p['lots'],
                    price=p['price'], sl=p['sl'], tp=p['tp'], strat_sl_d=stop_d, sl_d=p['sl_d'], tp_d=p['tp_d'],
                    equity=p['equity'], risk_usd=p['risk_usd'], risk_pct=p['risk_pct'], note=p['note'])
        if p['lots'] <= 0:
            self.db.order(**base, retcode=None, retmsg='skipped', ticket=None)
            log.info('%s SKIP %s', s['tag'], p['note']); return
        req = self.request(s, side, p)
        if MODE == 'live':
            r = self.rcall('order_send', req)
            rc = r.retcode if r else None; msg = r.comment if r else str(self.mt5.last_error())
            ticket = r.order if r else None
            self.db.order(**base, retcode=rc, retmsg=msg, ticket=ticket)
            log.info('%s LIVE %s %.2f lots @ %.2f sl %.2f tp %.2f risk $%.2f -> %s %s [%s]', s['tag'], 'BUY' if side > 0 else 'SELL',
                     p['lots'], p['price'], p['sl'], p['tp'], p['risk_usd'], rc, msg, p['note'])
        else:
            r = self.rcall('order_check', req)
            rc = r.retcode if r else None; msg = r.comment if r else str(self.mt5.last_error())
            oid = self.db.order(**base, retcode=rc, retmsg=f'order_check: {msg}', ticket=None)
            self.virtual[s['tag']] = dict(side=side, entry=p['price'], sl=p['sl'], tp=p['tp'], lots=p['lots'],
                                          t0=now_min, oid=oid, sym=p['sym'])
            log.info('%s SHADOW %s %.2f lots @ %.2f sl %.2f tp %.2f risk $%.2f (%s%%) check=%s %s [%s]', s['tag'], 'BUY' if side > 0 else 'SELL',
                     p['lots'], p['price'], p['sl'], p['tp'], p['risk_usd'], None if p['risk_pct'] is None else round(p['risk_pct'], 2), rc, msg, p['note'])

    def manage(self, s, B, now_min):
        mt5 = self.mt5; hold = (int(s['max_hold']) + 1) * 60
        if MODE == 'live':
            for pos in self.open_positions(s):
                if time.time() - pos.time >= hold:
                    tick = mt5.symbol_info_tick(pos.symbol)
                    close_side = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=pos.symbol, volume=pos.volume, type=close_side, position=pos.ticket,
                               price=tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask, deviation=200, magic=pos.magic,
                               comment='time exit', type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_FOK)
                    r = self.rcall('order_send', req)
                    self.db.exit(ts=time.time(), mode=MODE, tag=s['tag'], ticket=pos.ticket, reason=f'time ({r.retcode if r else None})',
                                 price=req['price'], pnl=pos.profit, order_id=None)
                    log.info('%s LIVE time exit ticket %s -> %s', s['tag'], pos.ticket, r.retcode if r else mt5.last_error())
            return
        v = self.virtual.get(s['tag'])
        if not v:
            return
        spread = (lambda t: t.ask - t.bid)(mt5.symbol_info_tick(v['sym']))
        m = B['t'] >= v['t0']
        for t, h, l, c in zip(B['t'][m], B['h'][m], B['l'][m], B['c'][m]):   # MT5 bars are bid prices
            hi_close = h if v['side'] > 0 else h + spread                      # price a close would execute at
            lo_close = l if v['side'] > 0 else l + spread
            reason = px = None
            if v['side'] > 0:
                if lo_close <= v['sl']: reason, px = 'stop', v['sl']
                elif hi_close >= v['tp']: reason, px = 'target', v['tp']
            else:
                if hi_close >= v['sl']: reason, px = 'stop', v['sl']
                elif lo_close <= v['tp']: reason, px = 'target', v['tp']
            if reason is None and (t + 1 - v['t0']) * 60 >= hold:
                reason, px = 'time', (c if v['side'] > 0 else c + spread)
            if reason:
                pnl = v['side'] * (px - v['entry']) * v['lots']
                self.db.exit(ts=time.time(), mode=MODE, tag=s['tag'], ticket=None, reason=reason, price=px, pnl=pnl, order_id=v['oid'])
                log.info('%s SHADOW exit %s @ %.2f pnl $%.2f', s['tag'], reason, px, pnl)
                del self.virtual[s['tag']]
                return

    def run(self):
        log.info('starting mt5_runner: mode=%s risk=%.2f%% strategies=%d', MODE, RISK_PCT, len(STRATS))
        while True:
            try:
                if self.mt5 is None:
                    self.connect()
                self.step()
            except Exception:
                log.error('loop error: %s', traceback.format_exc())
                try:
                    self.conn and self.conn.close()
                except Exception:
                    pass
                self.conn = self.mt5 = None
                time.sleep(10)
            time.sleep(62 - time.time() % 60)        # run ~2 s after each minute


if __name__ == '__main__':
    Runner().run()
