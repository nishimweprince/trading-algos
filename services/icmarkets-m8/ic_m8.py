"""IC Markets execution service: Money8gg Drift Signals rebuild on IC synthetics (the 4 combinations with t > 2).

Strategies (default Money8gg settings: momentum lookback 55, 1.5 ATR(14) move, 3-bar hold, 30-bar cooldown,
direction must alternate; stop 5 ATR, stop to entry after a 4 ATR move in favour (live: checked on every tick, polled every
IC_BE_POLL seconds, default 1; if price is already back through entry the position is closed); an opposite signal closes
and reverses):
  IC-B1000-M5-RS    Boom1000 M5, reverse + stop        magic 709001
  IC-B1000-M5-TP1   Boom1000 M5, take profit at 1R     magic 709002
  IC-B1000-M5-TP2   Boom1000 M5, take profit at 2R     magic 709003
  IC-J50-M1-RS      Jump50 M1, reverse + stop          magic 709004

Mode (IC_MODE in .env): shadow = every order goes to order_check only and trades are tracked virtually;
live = real orders on the account in IC_LOGIN. Kill file data/KILL_IC stops new entries (open positions keep their
broker SL/TP). Size: IC_LOTS (default 0.1 = the broker minimum).
"""
import json, logging, math, os, sqlite3, sys, time
import numpy as np
import rpyc

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data'); LOGS = os.path.join(HERE, 'logs')
os.makedirs(DATA, exist_ok=True); os.makedirs(LOGS, exist_ok=True)


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env(os.path.join(HERE, '.env'))
MODE = ENV.get('IC_MODE', 'shadow').lower()
LOTS = float(ENV.get('IC_LOTS', '0.1'))                  # used only when IC_RISK_PCT is 0 (fixed size)
RISK_PCT = float(ENV.get('IC_RISK_PCT', '5'))           # % of equity risked per trade (stop distance x lots)
MAX_RISK_PCT = float(ENV.get('IC_MAX_RISK_PCT', '10'))  # if even the minimum lot risks more than this, skip the trade
SHADOW_EQUITY = float(ENV.get('IC_SHADOW_EQUITY', '150'))
# Win ladder (research/sizing_part4.py): risk steps up one rung after each win of this strategy and drops back to the first
# rung after a loss; scratches (break-even exits) leave it unchanged. Empty = off (flat IC_RISK_PCT).
LADDER = [float(x) for x in ENV.get('IC_LADDER', '').split(',') if x.strip()]
LADDER_WIN_R = float(ENV.get('IC_LADDER_WIN_R', '0.05'))      # result >= this x risk counts as a win
LADDER_LOSS_R = float(ENV.get('IC_LADDER_LOSS_R', '-0.3'))    # result <= this x risk counts as a loss
LOGIN = ENV.get('IC_LOGIN', '')
SERVER = ENV.get('IC_SERVER', 'ICMarketsSC-MT5-3')
BRIDGE = ('127.0.0.1', int(ENV.get('IC_PORT', '18814')))
TERMINAL = ENV.get('IC_TERMINAL', r'C:\IC Markets Exec\terminal64.exe')
HIST = int(ENV.get('IC_HIST_MIN', '20000'))
BE_POLL = float(ENV.get('IC_BE_POLL', '1.0'))       # seconds between tick-level break-even checks (live)
P = dict(lookback=55, min_move=1.5, hold=3, cooldown=30, stop_atr=5.0, be_atr=4.0, atr_n=14)
STRATS = [
    dict(tag='IC-B1000-M5-RS', symbol='Boom1000', tf=5, mode=0, magic=709001),
    dict(tag='IC-B1000-M5-TP1', symbol='Boom1000', tf=5, mode=1, magic=709002),
    dict(tag='IC-B1000-M5-TP2', symbol='Boom1000', tf=5, mode=2, magic=709003),
    dict(tag='IC-J50-M1-RS', symbol='Jump50', tf=1, mode=0, magic=709004),
]
for s in STRATS:
    s['enabled'] = ENV.get(f"IC_ENABLE_{s['tag'].replace('-', '_')}", '1') == '1'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler(os.path.join(LOGS, 'ic_m8.log')), logging.StreamHandler(sys.stdout)])
log = logging.getLogger('ic_m8')


# ---------------- signal engine (identical to the backtest in m8.py) ----------------
def wilder_atr(h, l, c, n):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    a = np.full(len(c), np.nan)
    if len(c) < n: return a
    a[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        a[i] = a[i - 1] + (tr[i] - a[i - 1]) / n
    return a


def signals(c, a, lookback, min_move, hold, cooldown):
    n = len(c); sig = np.zeros(n, np.int64)
    bull = bear = 0; last_bar = -10 ** 9; last_dir = 0
    for i in range(n):
        if i < lookback or np.isnan(a[i]) or a[i] <= 0:
            continue
        m = c[i] - c[i - lookback]
        d = 1 if m > min_move * a[i] else (-1 if m < -min_move * a[i] else 0)
        bull = bull + 1 if d == 1 else 0
        bear = bear + 1 if d == -1 else 0
        can = i - last_bar >= cooldown
        if bull >= hold and can and last_dir != 1:
            sig[i] = 1; last_dir = 1; last_bar = i
        elif bear >= hold and can and last_dir != -1:
            sig[i] = -1; last_dir = -1; last_bar = i
    return sig


def resample(t, o, h, l, c, tf, now_min):
    """tf bars from closed M1 bars; only bars whose window has fully closed (start + tf <= now_min)."""
    g = t // tf
    st = np.flatnonzero(np.r_[True, g[1:] != g[:-1]]); en = np.r_[st[1:], len(t)]
    bt = g[st] * tf; hh = np.maximum.reduceat(h, st); ll = np.minimum.reduceat(l, st)
    ok = bt + tf <= now_min
    return dict(t=bt[ok], o=o[st][ok], c=c[en - 1][ok], h=hh[ok], l=ll[ok])


# ---------------- storage ----------------
class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'ic_m8.db'), isolation_level=None)
        self.c.execute('create table if not exists signals(id integer primary key, ts real, tag text, bar_t int, side int, atr real, close real, action text)')
        self.c.execute('''create table if not exists orders(id integer primary key, ts real, mode text, tag text, symbol text, kind text, side int,
                          lots real, price real, sl real, tp real, R real, atr real, retcode int, retmsg text, ticket int, note text)''')
        self.c.execute('''create table if not exists vpos2(tag text primary key, side int, entry real, sl real, tp real, R real, be_dist real,
                          be_done int, open_t int, last_ms int)''')
        self.c.execute('''create table if not exists vtrades(id integer primary key, tag text, side int, open_t int, close_t int, entry real,
                          exit real, R_mult real, usd real, reason text)''')
        self.c.execute('create table if not exists state(k text primary key, v text)')
        cols = [r[1] for r in self.c.execute('pragma table_info(vpos2)')]
        if 'lots' not in cols:
            self.c.execute('alter table vpos2 add column lots real')

    def ins(self, table, **k):
        self.c.execute(f"insert into {table}({','.join(k)}) values({','.join('?' * len(k))})", tuple(k.values()))

    def get(self, k, d=None):
        r = self.c.execute('select v from state where k=?', (k,)).fetchone(); return r[0] if r else d

    def put(self, k, v):
        self.c.execute('insert or replace into state(k, v) values(?, ?)', (k, str(v)))

    def last_order(self, tag, ticket):
        return self.c.execute('select R, atr from orders where tag=? and ticket=? order by id desc limit 1', (tag, ticket)).fetchone()


# ---------------- runner ----------------
class Runner:
    def __init__(self):
        self.db = DB(); self.conn = None; self.mt5 = None; self.off = {}
        self.be_seen = {}                            # live ticket -> last tick time_msc checked for break-even

    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE); self.conn._config['sync_request_timeout'] = 120
        self.conn.execute('import MetaTrader5 as _m5')
        self.mt5 = self.conn.modules.MetaTrader5
        self.conn.execute(f'_ok = _m5.initialize(path={TERMINAL!r}, portable=True, timeout=60000)')
        if not self.conn.eval('_ok'):
            raise RuntimeError(f'initialize failed: {self.conn.eval("str(_m5.last_error())")}')
        a = self.mt5.account_info()
        if str(a.login) != LOGIN or a.server != SERVER:
            raise RuntimeError(f'attached to {a.login}@{a.server}, expected {LOGIN}@{SERVER}')
        for s in {x['symbol'] for x in STRATS}:
            self.mt5.symbol_select(s, True)
        ti = self.mt5.terminal_info()
        log.info('connected: %s@%s mode=%s sizing=%s balance=%.2f equity=%.2f free margin=%.2f algo_trading=%s', a.login, a.server, MODE.upper(),
                 (f"ladder {'/'.join(f'{x:g}' for x in LADDER)}% risk (min-lot cap {MAX_RISK_PCT:g}%)" if LADDER else
                  f'{RISK_PCT:g}% risk (min-lot cap {MAX_RISK_PCT:g}%)' if RISK_PCT > 0 else f'{LOTS} lots'), a.balance, a.equity, a.margin_free, ti.trade_allowed)
        if MODE == 'live' and not ti.trade_allowed:
            log.error('Algo Trading is OFF in the terminal: live orders will be rejected until it is switched on')

    def rcall(self, fn, req):
        plain = {k: (v if isinstance(v, str) else float(v) if isinstance(v, (float, np.floating)) else int(v)) for k, v in req.items()}
        self.conn.execute(f'_req = {plain!r}')
        self.conn.execute(f'_res = _m5.{fn}(_req)')
        return self.conn.eval('_res')

    def m1(self, sym, now_min):
        self.conn.execute(f'_r = _m5.copy_rates_from_pos({sym!r}, _m5.TIMEFRAME_M1, 0, {HIST})')
        r = rpyc.classic.obtain(self.conn.eval('_r'))
        if r is None or len(r) == 0:
            return None
        t = (r['time'] // 60).astype(np.int64); m = t < now_min
        return dict(t=t[m], o=r['open'][m].astype(float), h=r['high'][m].astype(float), l=r['low'][m].astype(float), c=r['close'][m].astype(float))

    def ticks_since(self, sym, ms):
        """Bid/ask ticks strictly after ms (epoch milliseconds), oldest first."""
        self.conn.execute(f'_tk = _m5.copy_ticks_from({sym!r}, {int(ms // 1000)}, 200000, _m5.COPY_TICKS_INFO)')
        r = rpyc.classic.obtain(self.conn.eval('_tk'))
        if r is None or len(r) == 0:
            return np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
        m = r['time_msc'] > ms
        return r['time_msc'][m].astype(np.int64), r['bid'][m].astype(float), r['ask'][m].astype(float)

    def positions(self, s):
        return [x for x in (self.mt5.positions_get(symbol=s['symbol']) or []) if x.magic == s['magic']]

    # ---- orders ----
    def deal(self, s, side, kind, position=None, volume=None, sl=0.0, tp=0.0, R=None, atr=None, note=''):
        mt5 = self.mt5; info = mt5.symbol_info(s['symbol']); tick = mt5.symbol_info_tick(s['symbol'])
        price = tick.ask if side > 0 else tick.bid
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=s['symbol'], volume=float(volume or LOTS),
                   type=mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL, price=float(price), sl=float(sl), tp=float(tp),
                   deviation=500, magic=int(s['magic']), comment=s['tag'][:31], type_time=mt5.ORDER_TIME_GTC,
                   type_filling=mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else mt5.ORDER_FILLING_IOC)
        if position is not None:
            req['position'] = int(position)
        live = MODE == 'live'
        r = self.rcall('order_send' if live else 'order_check', req)
        rc = r.retcode if r else None; msg = r.comment if r else str(mt5.last_error())
        ticket = (getattr(r, 'order', None) if kind == 'open' else position) if live and r else None
        self.db.ins('orders', ts=time.time(), mode=MODE, tag=s['tag'], symbol=s['symbol'], kind=kind, side=side, lots=req['volume'], price=price,
                    sl=sl, tp=tp, R=R, atr=atr, retcode=rc, retmsg=msg, ticket=ticket, note=note)
        log.info('%s %s %s %s %.2f @ %.3f sl %.3f tp %.3f -> %s %s %s', s['tag'], MODE.upper(), kind, 'BUY' if side > 0 else 'SELL',
                 req['volume'], price, sl, tp, rc, msg, note)
        ok = (rc == mt5.TRADE_RETCODE_DONE) if live else (rc == 0)
        return ok, r, price

    def equity(self):
        if MODE == 'live':
            a = self.mt5.account_info(); return float(a.equity), float(a.margin_free)
        eq = float(self.db.get('shadow_equity', SHADOW_EQUITY)); return eq, eq

    def closed_results(self, s):
        """This strategy's closed trades, oldest first, as result / risk at entry (R multiples). Live: broker deals grouped
        by position (closing deals of manual or stop-out exits may carry magic 0, so the opening deal decides the strategy);
        shadow: the vtrades table."""
        if MODE != 'live':
            return [r[0] for r in self.db.c.execute('select R_mult from vtrades where tag=? and R_mult is not null order by id', (s['tag'],))]
        self.conn.execute('import datetime as _dt')
        self.conn.execute('_hd = _m5.history_deals_get(_dt.datetime.now() - _dt.timedelta(days=30), _dt.datetime.now() + _dt.timedelta(days=2))')
        self.conn.execute('_hd = [(x.position_id, x.entry, x.magic, x.volume, x.profit + x.commission + x.swap + x.fee, x.time_msc) '
                          'for x in (_hd or []) if x.type in (0, 1)]')
        pos = {}
        for pid, entry, magic, vol, pnl, tms in rpyc.classic.obtain(self.conn.eval('_hd')):
            p = pos.setdefault(pid, dict(magic=None, vin=0.0, vout=0.0, pnl=0.0, t=0))
            if entry == 0:
                p['magic'] = magic; p['vin'] += vol
            else:
                p['vout'] += vol; p['t'] = max(p['t'], tms)
            p['pnl'] += pnl
        val = None; out = []
        for pid, p in sorted(pos.items(), key=lambda kv: kv[1]['t']):
            if p['magic'] != s['magic'] or p['vin'] <= 0 or p['vout'] + 1e-9 < p['vin']:
                continue
            lo = self.db.last_order(s['tag'], pid)
            if not lo or not lo[0]:
                continue
            if val is None:
                info = self.mt5.symbol_info(s['symbol']); val = info.trade_tick_value / info.trade_tick_size
            out.append(p['pnl'] / (lo[0] * p['vin'] * val))
        return out

    def ladder_rung(self, s):
        k = 0
        for r in self.closed_results(s):
            if r >= LADDER_WIN_R:
                k += 1
            elif r <= LADDER_LOSS_R:
                k = 0
        return min(k, len(LADDER) - 1)

    def risk_for(self, s, side):
        """Risk % for this strategy and direction: IC_RISK_PCT_<TAG>_<LONG|SHORT>, then IC_RISK_PCT_<TAG>, then the win
        ladder (IC_LADDER), then IC_RISK_PCT."""
        e = load_env(os.path.join(HERE, '.env')); key = 'IC_RISK_PCT_' + s['tag'].replace('-', '_')
        v = e.get(f"{key}_{'LONG' if side > 0 else 'SHORT'}", e.get(key))
        if v not in (None, ''):
            return float(v)
        if LADDER:
            k = self.ladder_rung(s); log.info('%s ladder rung %d/%d -> %g%% risk', s['tag'], k + 1, len(LADDER), LADDER[k])
            return LADDER[k]
        return RISK_PCT

    def size(self, s, R, side, price):
        """Lots so that a stop-out loses RISK_PCT of equity; minimum lot if that is too small but still within MAX_RISK_PCT;
        capped by free margin. Returns (lots, note) with lots 0 when the trade must be skipped."""
        mt5 = self.mt5; info = mt5.symbol_info(s['symbol']); val = info.trade_tick_value / info.trade_tick_size
        eq, free = self.equity(); step, vmin = info.volume_step, info.volume_min
        pct = self.risk_for(s, side); cap = max(MAX_RISK_PCT, pct)
        if pct <= 0:
            lots = LOTS; note = 'fixed size'
        else:
            lots = math.floor(eq * pct / 100.0 / (R * val) / step + 1e-9) * step; note = f'{pct:g}% of ${eq:.2f}'
            if lots < vmin:
                if vmin * R * val <= eq * cap / 100.0:
                    lots = vmin; note += f'; min lot risks {100 * vmin * R * val / eq:.1f}%'
                else:
                    return 0.0, f'min lot risks ${vmin * R * val:.2f} = {100 * vmin * R * val / max(eq, 1e-9):.0f}% of ${eq:.2f} (> {cap:g}%)'
        m_lot = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL, s['symbol'], vmin, price)
        if m_lot:
            cap = math.floor(free * 0.9 / (m_lot / vmin) / step + 1e-9) * step
            if cap < lots:
                lots = cap; note += f'; cut to {cap:.2f} by margin'
        lots = round(min(lots, info.volume_max), 2)
        if lots < vmin:
            return 0.0, f'not enough free margin (${free:.2f}) for the minimum lot'
        return lots, note

    def open_trade(self, s, side, atr_k, last_close):
        mt5 = self.mt5; info = mt5.symbol_info(s['symbol']); tick = mt5.symbol_info_tick(s['symbol'])
        R = P['stop_atr'] * atr_k; price = tick.ask if side > 0 else tick.bid
        age = time.time() + self.off.get(s['symbol'], 0) - tick.time_msc / 1000.0; gap = abs(tick.bid - last_close)
        if age > 15 or gap > 3 * R:                  # stale or implausible quote: do not trade on it
            self.db.ins('orders', ts=time.time(), mode=MODE, tag=s['tag'], symbol=s['symbol'], kind='open', side=side, lots=LOTS, price=price,
                        sl=None, tp=None, R=R, atr=atr_k, retcode=None, retmsg='skipped', ticket=None,
                        note=f'stale quote: age {age:.0f}s, bid {tick.bid} vs bar close {last_close}')
            log.warning('%s SKIP stale quote: tick age %.0fs, bid %.3f vs last close %.3f', s['tag'], age, tick.bid, last_close); return
        sl = round(price - side * R, info.digits)
        tp = round(price + side * R * s['mode'], info.digits) if s['mode'] in (1, 2) else 0.0
        lots, note = self.size(s, R, side, price)
        if lots <= 0:
            self.db.ins('orders', ts=time.time(), mode=MODE, tag=s['tag'], symbol=s['symbol'], kind='open', side=side, lots=0, price=price,
                        sl=sl, tp=tp, R=R, atr=atr_k, retcode=None, retmsg='skipped', ticket=None, note=note)
            log.warning('%s SKIP: %s', s['tag'], note); return
        val = info.trade_tick_value / info.trade_tick_size
        ok, r, px = self.deal(s, side, 'open', volume=lots, sl=sl, tp=tp, R=R, atr=atr_k, note=f'risk ${lots * R * val:.2f} ({note})')
        if MODE == 'live' and ok:                    # re-anchor SL/TP on the actual fill
            time.sleep(0.5)
            for pos in self.positions(s):
                e = pos.price_open; nsl = round(e - side * R, info.digits)
                ntp = round(e + side * R * s['mode'], info.digits) if s['mode'] in (1, 2) else 0.0
                if abs(nsl - pos.sl) > info.point or abs(ntp - pos.tp) > info.point:
                    self.rcall('order_send', dict(action=mt5.TRADE_ACTION_SLTP, symbol=s['symbol'], position=int(pos.ticket), sl=float(nsl), tp=float(ntp), magic=int(s['magic'])))
                self.db.c.execute('update orders set ticket=? where id=(select max(id) from orders where tag=? and kind="open")', (pos.ticket, s['tag']))
        if MODE != 'live':                           # shadow: virtual position at the quote the order was checked at
            ms = int(self.mt5.symbol_info_tick(s['symbol']).time_msc)
            self.db.c.execute('insert or replace into vpos2(tag, side, entry, sl, tp, R, be_dist, be_done, open_t, last_ms, lots) values(?,?,?,?,?,?,?,?,?,?,?)',
                              (s['tag'], side, px, px - side * R, (px + side * R * s['mode']) if s['mode'] in (1, 2) else 0.0, R,
                               P['be_atr'] * atr_k, 0, int(time.time() // 60), ms, lots))

    def close_all(self, s, why):
        if MODE == 'live':
            for pos in self.positions(s):
                side = 1 if pos.type == self.mt5.POSITION_TYPE_BUY else -1
                self.deal(s, -side, 'close', position=pos.ticket, volume=pos.volume, note=why)
        else:
            v = self.db.c.execute('select side, entry, R, open_t from vpos2 where tag=?', (s['tag'],)).fetchone()
            if v:
                tick = self.mt5.symbol_info_tick(s['symbol']); side, e, R, ot = v
                px = tick.bid if side > 0 else tick.ask
                self.vclose(s, px, why)

    def vclose(self, s, px, why):
        side, e, R, ot, lots = self.db.c.execute('select side, entry, R, open_t, coalesce(lots, ?) from vpos2 where tag=?', (LOTS, s['tag'])).fetchone()
        pnl = side * (px - e); usd = pnl * lots
        self.db.ins('vtrades', tag=s['tag'], side=side, open_t=ot, close_t=int(time.time() // 60), entry=e, exit=px,
                    R_mult=pnl / R if R else None, usd=usd, reason=why)
        self.db.c.execute('delete from vpos2 where tag=?', (s['tag'],))
        eq = float(self.db.get('shadow_equity', SHADOW_EQUITY)) + usd; self.db.put('shadow_equity', round(eq, 2))
        log.info('%s SHADOW close %s %.2f lots @ %.3f (%s) %+.3fR $%+.2f -> shadow equity $%.2f', s['tag'], 'long' if side > 0 else 'short',
                 lots, px, why, pnl / R if R else 0, usd, eq)

    # ---- management ----
    def manage(self, s, B):
        mt5 = self.mt5; tick = mt5.symbol_info_tick(s['symbol']); spread = tick.ask - tick.bid
        if MODE == 'live':
            info = mt5.symbol_info(s['symbol'])
            for pos in self.positions(s):
                side = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
                lo = self.db.last_order(s['tag'], pos.ticket); atr_k = lo[1] if lo and lo[1] else None
                if atr_k is None or (side > 0 and pos.sl >= pos.price_open) or (side < 0 and 0 < pos.sl <= pos.price_open):
                    continue
                _, bid, ask = self.ticks_since(s['symbol'], int(pos.time_msc))   # only prices after the fill
                best = max(float(bid.max()) if len(bid) else pos.price_open, tick.bid) if side > 0 else \
                    min(float(ask.min()) if len(ask) else pos.price_open, tick.ask)
                if side * (best - pos.price_open) >= P['be_atr'] * atr_k:
                    new = round(pos.price_open, info.digits)
                    r = self.rcall('order_send', dict(action=mt5.TRADE_ACTION_SLTP, symbol=s['symbol'], position=int(pos.ticket), sl=float(new), tp=float(pos.tp), magic=int(s['magic'])))
                    log.info('%s breakeven %s sl %.3f -> %.3f (%s)', s['tag'], pos.ticket, pos.sl, new, r.retcode if r else mt5.last_error())
        else:
            v = self.db.c.execute('select side, entry, sl, tp, R, be_dist, be_done, open_t, last_ms from vpos2 where tag=?', (s['tag'],)).fetchone()
            if not v:
                return
            side, e, sl, tp, R, be, be_done, ot, last_ms = v
            T, bid, ask = self.ticks_since(s['symbol'], last_ms)        # replay every tick after the fill, in order
            for k in range(len(T)):
                px = bid[k] if side > 0 else ask[k]                         # buys exit on bid, sells on ask
                if (side > 0 and px <= sl) or (side < 0 and px >= sl):
                    return self.vclose(s, px, 'stop' if not be_done else 'breakeven')
                if tp and side * (px - tp) >= 0:
                    return self.vclose(s, px, 'target')
                if not be_done and side * (px - e) >= be:
                    sl = e; be_done = 1
                    self.db.c.execute('update vpos2 set sl=?, be_done=1 where tag=?', (e, s['tag']))
                    log.info('%s SHADOW breakeven sl -> %.3f', s['tag'], e)
            if len(T):
                self.db.c.execute('update vpos2 set last_ms=? where tag=?', (int(T[-1]), s['tag']))

    def be_watch(self):
        """Live only: move the stop to entry on the first tick whose exit-side price is be_atr x ATR in favour.
        Every tick since the last check is read, so a touch between polls is not missed. If price has already come
        back through entry, a stop at entry is no longer valid, so the position is closed at market (the result a
        tick-level break-even stop would have given)."""
        if MODE != 'live':
            return
        mt5 = self.mt5
        for s in [x for x in STRATS if x['enabled']]:
            for pos in self.positions(s):
                side = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
                if (side > 0 and pos.sl >= pos.price_open) or (side < 0 and 0 < pos.sl <= pos.price_open):
                    self.be_seen.pop(pos.ticket, None); continue          # already at break-even
                lo = self.db.last_order(s['tag'], pos.ticket); atr_k = lo[1] if lo and lo[1] else None
                if atr_k is None:
                    continue
                since = self.be_seen.get(pos.ticket, int(pos.time_msc))
                T, bid, ask = self.ticks_since(s['symbol'], since)
                if not len(T):
                    continue
                px = bid if side > 0 else ask
                hit = np.flatnonzero(side * (px - pos.price_open) >= P['be_atr'] * atr_k)
                if not len(hit):
                    self.be_seen[pos.ticket] = int(T[-1]); continue      # only advance when nothing triggered
                info = mt5.symbol_info(s['symbol']); tick = mt5.symbol_info_tick(s['symbol'])
                now_px = tick.bid if side > 0 else tick.ask; new = round(pos.price_open, info.digits)
                when = int(T[hit[0]])
                if side * (now_px - new) > info.point * max(info.trade_stops_level, 1):
                    r = self.rcall('order_send', dict(action=mt5.TRADE_ACTION_SLTP, symbol=s['symbol'], position=int(pos.ticket),
                                                      sl=float(new), tp=float(pos.tp), magic=int(s['magic'])))
                    rc = r.retcode if r else None
                    log.info('%s breakeven (tick %d, %.1fs ago) %s sl %.3f -> %.3f: %s %s', s['tag'], when,
                             (int(T[-1]) - when) / 1000, pos.ticket, pos.sl, new, rc, r.comment if r else mt5.last_error())
                    if rc == mt5.TRADE_RETCODE_DONE:
                        self.be_seen.pop(pos.ticket, None); continue
                    tick = mt5.symbol_info_tick(s['symbol']); now_px = tick.bid if side > 0 else tick.ask
                    if side * (now_px - new) > 0:
                        self.be_seen[pos.ticket] = when - 1; continue    # modification failed, price still in profit: retry next poll
                self.deal(s, -side, 'close', position=pos.ticket, volume=pos.volume,
                          note=f'breakeven: {P["be_atr"]:g} ATR reached at tick {when}, price back through entry before the stop could move')
                self.be_seen.pop(pos.ticket, None)

    # ---- loop ----
    def server_offset(self, sym):
        """IC labels bars and ticks in its server time (UTC+2/+3): offset in seconds, from the latest tick, to the nearest hour."""
        t = self.mt5.symbol_info_tick(sym)
        if t is None or not t.time_msc:
            return None
        off = int(round((t.time_msc / 1000.0 - time.time()) / 3600.0) * 3600)
        lag = time.time() + off - t.time_msc / 1000.0
        if lag > 30:
            log.warning('%s: latest quote is %.0fs old', sym, lag)
        return off

    def step(self):
        kill = os.path.exists(os.path.join(DATA, 'KILL_IC'))
        for sym in sorted({s['symbol'] for s in STRATS}):
            off = self.server_offset(sym)
            if off is None:
                self.mt5.symbol_select(sym, True); log.warning('%s: no quote yet', sym); continue
            if self.off.get(sym) != off:
                log.info('%s: server clock offset %+d h', sym, off // 3600)
            self.off[sym] = off
            now = int((time.time() + off) // 60)                # current minute in IC server time
            B = self.m1(sym, now)
            if B is None or len(B['t']) < 1000:
                log.warning('%s: not enough history', sym); continue
            for s in [x for x in STRATS if x['symbol'] == sym and x['enabled']]:
                b = resample(B['t'], B['o'], B['h'], B['l'], B['c'], s['tf'], now)
                a = wilder_atr(b['h'], b['l'], b['c'], P['atr_n'])
                sig = signals(b['c'], a, P['lookback'], P['min_move'], P['hold'], P['cooldown'])
                k = len(b['t']) - 1; bar_t = int(b['t'][k])
                key = f"last_bar:{s['tag']}"
                if self.db.get(key) is None:          # first run: never act on a signal from before start-up
                    self.db.put(key, bar_t); log.info('%s armed at bar %s (last signal %s)', s['tag'], bar_t,
                                                       int(b['t'][np.flatnonzero(sig)[-1]]) if sig.any() else None)
                if bar_t > int(self.db.get(key)):
                    self.db.put(key, bar_t)
                    fresh = now - (bar_t + s['tf']) <= 2
                    if sig[k] != 0 and fresh:
                        side = int(sig[k])
                        act = 'killed' if kill else 'reverse+open'
                        self.db.ins('signals', ts=time.time(), tag=s['tag'], bar_t=bar_t, side=side, atr=float(a[k]), close=float(b['c'][k]), action=act)
                        log.info('%s SIGNAL %s at bar %s close %.3f atr %.3f -> %s', s['tag'], 'LONG' if side > 0 else 'SHORT', bar_t, b['c'][k], a[k], act)
                        self.close_all(s, 'reverse signal')
                        if not kill:
                            self.open_trade(s, side, float(a[k]), float(B['c'][-1]))
                self.manage(s, B)

    def run(self):
        while True:
            try:
                if self.conn is None:
                    self.connect()
                self.step()
                wake = time.time() + max(1.0, 61 - time.time() % 60)   # next step ~1 s after the minute closes
                while time.time() < wake - BE_POLL:                     # meanwhile: tick-level break-even (live)
                    self.be_watch(); time.sleep(BE_POLL)
            except Exception as e:
                log.exception('loop error: %s', e); self.conn = None; time.sleep(20); continue
            time.sleep(max(0.0, wake - time.time()))


def status():
    db = DB()
    print('mode', MODE, 'risk %', RISK_PCT, 'shadow equity', db.get('shadow_equity', SHADOW_EQUITY))
    for r in db.c.execute('select tag, count(*), sum(R_mult), sum(usd) from vtrades group by tag'):
        print('shadow', r)
    for r in db.c.execute('select * from vpos2'):
        print('shadow open', r)
    for r in db.c.execute('select datetime(ts,"unixepoch"), tag, kind, side, lots, price, sl, tp, retcode, retmsg, note from orders order by id desc limit 10'):
        print(r)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'status':
        status()
    else:
        Runner().run()
