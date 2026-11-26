"""Prospective paper tests of the two t >= 2 walk-forward leads (research/dsi_analogs, 2026-11-26).

Rules, fixed before the test and identical to research/dsi_analogs/wf.py:
  flip1000_run   Crash Boom Flip 1000, family run4, follow, 1800 s hold.
                 Event: the 4th consecutive 1 s mid move in the same direction; trade in that direction.
  trekup_vs600   Trek Up, family vshock3, follow, 600 s hold (primary for this lead).
  trekup_vs1800  Same events, 1800 s hold (secondary, reported only).
                 Event: 60 s realised variance of jump-free 1 s returns first exceeds 3 x its level over the
                 preceding 1800 s; trade in the direction of the last 60 s jump-free move.
                 (Jump-free: 1 s returns above 8 robust sigma set to 0; sigma from the last 24 h, refreshed hourly.)
Common: 1 s grid of Deriv MT5 quotes (last tick per second, valid if < 5 s old); entry 1 s after the event at the
ask (buy) / bid (sell); exit after the hold at the bid / ask; one position per rule; swap charged pro rata.
Fills are simulated on recorded ticks in ~20 s batches once the exit tick exists. No orders are sent.

Verdict (pre-registered): 2026-11-27 to 2027-02-19 UTC. A lead PASSES if its primary rule's mean net bp > 0 with
daily t >= 2. Events before the start are warm-up and excluded.
Data: data/leads.db. Log: logs/leads_paper.log. Kill switch: data/KILL_LEADS.
"""
import datetime as dt, logging, os, sqlite3, time, traceback
import numpy as np
import rpyc

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data'); LOGS = os.path.join(BASE, 'logs')
os.makedirs(DATA, exist_ok=True); os.makedirs(LOGS, exist_ok=True)

LEADS = {
    'flip1000_run': dict(symbol='Crash Boom Flip 1000 Index', kind='run', n=4, hold=1800),
    'trekup_vs600': dict(symbol='Trek Up Index', kind='vshock', q=3.0, hold=600),
    'trekup_vs1800': dict(symbol='Trek Up Index', kind='vshock', q=3.0, hold=1800),
}
K, LAT, STALE_MS, LOOK = 8.0, 1, 5000, 2200      # LOOK covers the 1860 s variance window + 300 s start guard
START = int(dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc).timestamp())
END = int(dt.datetime(2027, 2, 19, tzinfo=dt.timezone.utc).timestamp())
CHUNK, SIGMA_EVERY = 7200, 3600
BRIDGE = ('127.0.0.1', 18812)

logging.basicConfig(filename=os.path.join(LOGS, 'leads_paper.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('leads')


class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'leads.db'), isolation_level=None)
        self.c.execute('create table if not exists state(k text primary key, v text)')
        self.c.execute('''create table if not exists trades(id integer primary key, lead text, event_s int, side int, entry_s int,
            exit_s int, entry real, exit real, entry_spread real, exit_spread real, net_bps real, gross_bps real, pnl_minlot real, warmup int)''')
        self.c.execute('''create table if not exists daily(day text, lead text, events int, taken int, busy int, invalid int,
            primary key(day, lead))''')

    def get(self, k, d=None):
        r = self.c.execute('select v from state where k=?', (k,)).fetchone()
        return r[0] if r else d

    def put(self, k, v):
        self.c.execute('insert or replace into state values(?,?)', (k, str(v)))

    def count(self, day, lead, **kw):
        self.c.execute('insert or ignore into daily values(?,?,0,0,0,0)', (day, lead))
        for k, v in kw.items():
            if v:
                self.c.execute(f'update daily set {k}={k}+? where day=? and lead=?', (int(v), day, lead))


def grid(ms, bid, ask, a_s, b_s):
    secs = np.arange(a_s, b_s + 1)
    idx = np.searchsorted(ms, secs * 1000 + 999, 'right') - 1
    j = np.clip(idx, 0, None)
    valid = (idx >= 0) & (secs * 1000 + 999 - ms[j] < STALE_MS)
    return bid[j], ask[j], valid


def run_events(r, n):
    """Index of the second at which a run of same-sign nonzero 1 s moves reaches exactly n; side = run sign."""
    rl = np.zeros(len(r), np.int64)
    for i in range(1, len(r)):
        if r[i] != 0:
            rl[i] = rl[i - 1] + 1 if (r[i - 1] != 0 and (r[i] > 0) == (r[i - 1] > 0)) else 1
    t = np.flatnonzero(rl == n)
    return t, np.sign(r[t]).astype(np.int64)


def vshock_events(rj, Lj, q):
    c2 = np.cumsum(rj ** 2); S = len(rj)
    rv60 = np.full(S, np.nan); rv60[60:] = (c2[60:] - c2[:-60]) / 60
    rv1800 = np.full(S, np.nan); rv1800[1860:] = (c2[1800:-60] - c2[:-1860]) / 1800
    ratio = rv60 / np.maximum(rv1800, 1e-30)
    prev = np.r_[np.nan, ratio[:-1]]
    t = np.flatnonzero((ratio > q) & ~(prev > q)); t = t[t >= 300]
    sd = np.sign(Lj[t] - Lj[t - 60]).astype(np.int64); m = sd != 0
    return t[m], sd[m]


class Leads:
    def __init__(self):
        self.db = DB(); self.conn = None; self.m = None; self.info = {}

    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE); self.conn._config['sync_request_timeout'] = 120
        self.conn.execute('import MetaTrader5 as _m5'); self.m = self.conn.modules.MetaTrader5
        for n in {L['symbol'] for L in LEADS.values()}:
            self.m.symbol_select(n, True)
        time.sleep(1)
        for n in {L['symbol'] for L in LEADS.values()}:
            i = self.m.symbol_info(n)
            if i is None:
                raise RuntimeError(f'no symbol_info for {n}')
            self.info[n] = dict(val=i.trade_tick_value / i.trade_tick_size, vmin=i.volume_min,
                                swap_bps_s=abs(i.swap_long) / 100 / 31536000 * 1e4 if i.swap_mode == 5 else 0.0)
        if self.db.get('started') is None:
            self.db.put('started', time.time())
        log.info('connected; START %s; leads %s', dt.datetime.fromtimestamp(START, dt.timezone.utc), list(LEADS))

    def ticks(self, name, a_s, b_s):
        self.conn.execute(f'_tk = _m5.copy_ticks_range({name!r}, {int(a_s)}, {int(b_s)}, _m5.COPY_TICKS_ALL)\n'
                          f'_x = None if _tk is None or len(_tk) == 0 else (_tk["time_msc"].tobytes(), _tk["bid"].tobytes(), _tk["ask"].tobytes())')
        x = self.conn.eval('_x')
        if x is None:
            return None
        ms = np.frombuffer(bytes(x[0]), np.int64); bid = np.frombuffer(bytes(x[1]), np.float64); ask = np.frombuffer(bytes(x[2]), np.float64)
        ok = (bid > 0) & (ask > 0) & (ask >= bid)
        o = np.argsort(ms[ok], kind='stable')
        return ms[ok][o], bid[ok][o], ask[ok][o]

    def sigma(self, name, now_s):
        key = f'sigma:{name}'
        if self.db.get(key) and now_s - int(self.db.get(f'sigma_at:{name}', 0)) < SIGMA_EVERY:
            return float(self.db.get(key))
        t = self.ticks(name, now_s - 86400, now_s)
        if t is None:
            raise RuntimeError(f'no ticks for sigma {name}')
        b, a, v = grid(*t, now_s - 86400, now_s - 6)
        r = np.diff(np.log((a + b) / 2)); r[~(v[1:] & v[:-1])] = 0
        nz = r[r != 0]; s = float(np.median(np.abs(nz)) * 1.4826)
        self.db.put(key, s); self.db.put(f'sigma_at:{name}', now_s)
        log.info('%s sigma %.3e (%.3f bp)', name, s, s * 1e4)
        return s

    def process(self, key, L, now_s):
        G = now_s - 6; hold = L['hold']
        done = self.db.get(f'done:{key}')
        if done is None:
            self.db.put(f'done:{key}', G - LAT - hold); return
        done = int(done); busy = int(self.db.get(f'busy:{key}', 0))
        te_max = min(G - LAT - hold, done + CHUNK)
        if te_max <= done:
            return
        name = L['symbol']; a_s = done - LOOK
        tk = self.ticks(name, a_s - 10, G + 1)
        if tk is None:
            log.warning('%s no ticks %d..%d', key, a_s, G); return
        bid, ask, valid = grid(*tk, a_s, G)
        mid = (bid + ask) / 2
        r = np.r_[0.0, np.diff(np.log(mid))]; r[~valid] = 0; r[np.r_[False, ~valid[:-1]]] = 0
        if L['kind'] == 'run':
            ev, side = run_events(r, L['n'])
        else:
            sig = self.sigma(name, now_s)
            rj = np.where(np.abs(r) > K * sig, 0.0, r)
            ev, side = vshock_events(rj, np.cumsum(rj), L['q'])
        m = (ev + a_s > done) & (ev + a_s <= te_max); ev, side = ev[m], side[m]
        info = self.info[name]; cnt = {}
        for i, s in zip(ev, side):
            te = int(a_s + i); day = dt.datetime.fromtimestamp(te, dt.timezone.utc).strftime('%Y-%m-%d')
            c = cnt.setdefault(day, dict(events=0, taken=0, busy=0, invalid=0)); c['events'] += 1
            e = te + LAT; x = e + hold
            if e <= busy:
                c['busy'] += 1; continue
            ei, xi = e - a_s, x - a_s
            if xi >= len(mid) or not valid[ei] or not valid[xi]:
                c['invalid'] += 1; continue
            s = int(s)
            pe = ask[ei] if s > 0 else bid[ei]; px = bid[xi] if s > 0 else ask[xi]
            net = s * (px - pe) / pe * 1e4 - info['swap_bps_s'] * hold
            gross = s * (mid[xi] - mid[ei]) / mid[ei] * 1e4
            pnl = s * (px - pe) * info['vmin'] * info['val']
            self.db.c.execute('''insert into trades(lead, event_s, side, entry_s, exit_s, entry, exit, entry_spread, exit_spread,
                net_bps, gross_bps, pnl_minlot, warmup) values(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (key, te, s, e, x, float(pe), float(px), float(ask[ei] - bid[ei]), float(ask[xi] - bid[xi]),
                               float(net), float(gross), float(pnl), int(te < START)))
            c['taken'] += 1; busy = x
            log.info('%s %s %s @ %.4f -> %.4f  %+.3f bp  $%+.3f%s', key, dt.datetime.fromtimestamp(e, dt.timezone.utc).strftime('%m-%d %H:%M:%S'),
                     'BUY ' if s > 0 else 'SELL', pe, px, net, pnl, ' (warm-up)' if te < START else '')
        for day, c in cnt.items():
            self.db.count(day, key, **c)
        self.db.put(f'done:{key}', te_max); self.db.put(f'busy:{key}', busy); self.db.put(f'last_run:{key}', now_s)

    def run(self):
        while True:
            try:
                if os.path.exists(os.path.join(DATA, 'KILL_LEADS')):
                    time.sleep(30); continue
                if self.conn is None:
                    self.connect()
                now_s = int(time.time())
                for key, L in LEADS.items():
                    self.process(key, L, now_s)
            except Exception:
                log.error('loop error\n%s', traceback.format_exc()); self.conn = None; time.sleep(15)
            time.sleep(20)


if __name__ == '__main__':
    Leads().run()
