"""Prospective paper test of the Trek spike-flip fade (lead from research/dsi_analogs, 2026-11-26).

Rule, fixed before the test (identical to research/dsi_analogs/wf.py, family post120_flip, mode fade, 30 s):
  - 1-second grid of Deriv MT5 quotes (last tick in each second; a second is valid if its last tick is < 5 s old).
  - Spike: |1 s mid log return| > 8 robust sigma (sigma = 1.4826 x median |nonzero 1 s return| over the last 24 h).
  - 120 s after a spike, compare the jump-free drift since the spike with the jump-free drift over the 300 s before it.
    If both are non-zero and their signs differ (the spike appears to have flipped the drift), FADE the new drift.
  - Entry 1 s later (latency) at the ask (buy) / bid (sell); exit 30 s after entry at the bid / ask.
  - One position per symbol: an event whose entry falls inside an open trade is skipped.
Fills are simulated on recorded Deriv ticks in batches every ~20 s (the trade is complete before it is written).
No orders are sent. Events before START are marked warm-up and excluded from the verdict.

Verdict (pre-registered): run 2026-11-27 to 2026-12-25 UTC. PASS if the pooled mean net bp > 0 with daily t >= 2
and both Trek Up and Trek Down have a positive mean. Otherwise the lead is dropped.
Data: data/trek.db. Log: logs/trek_paper.log. Kill switch: data/KILL_TREK (stops processing).
"""
import datetime as dt, logging, os, sqlite3, time, traceback
import numpy as np
import rpyc

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data'); LOGS = os.path.join(BASE, 'logs')
os.makedirs(DATA, exist_ok=True); os.makedirs(LOGS, exist_ok=True)

SYMBOLS = {'TREK_U': 'Trek Up Index', 'TREK_D': 'Trek Down Index'}
K, T, PRE, HOLD, LAT, STALE_MS = 8.0, 120, 300, 30, 1, 5000
START = int(dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc).timestamp())
END = int(dt.datetime(2026, 12, 25, tzinfo=dt.timezone.utc).timestamp())
CHUNK = 7200            # max seconds of events processed per symbol per cycle (catch-up after downtime)
SIGMA_EVERY = 3600
BRIDGE = ('127.0.0.1', 18812)

logging.basicConfig(filename=os.path.join(LOGS, 'trek_paper.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('trek')


class DB:
    def __init__(self):
        self.c = sqlite3.connect(os.path.join(DATA, 'trek.db'), isolation_level=None)
        self.c.execute('create table if not exists state(k text primary key, v text)')
        self.c.execute('''create table if not exists trades(id integer primary key, code text, spike_s int, event_s int, side int,
            entry_s int, exit_s int, entry real, exit real, entry_spread real, exit_spread real, net_bps real, gross_bps real,
            pnl_minlot real, sigma real, warmup int)''')
        self.c.execute('''create table if not exists daily(day text, code text, spikes int, flips int, taken int, busy int, invalid int,
            primary key(day, code))''')

    def get(self, k, d=None):
        r = self.c.execute('select v from state where k=?', (k,)).fetchone()
        return r[0] if r else d

    def put(self, k, v):
        self.c.execute('insert or replace into state values(?,?)', (k, str(v)))

    def count(self, day, code, **kw):
        self.c.execute('insert or ignore into daily values(?,?,0,0,0,0,0)', (day, code))
        for k, v in kw.items():
            if v:
                self.c.execute(f'update daily set {k}={k}+? where day=? and code=?', (int(v), day, code))


def grid(ms, bid, ask, a_s, b_s):
    """Seconds a_s..b_s inclusive: last tick at or before the end of each second."""
    secs = np.arange(a_s, b_s + 1)
    idx = np.searchsorted(ms, secs * 1000 + 999, 'right') - 1
    ok = idx >= 0
    j = np.clip(idx, 0, None)
    valid = ok & (secs * 1000 + 999 - ms[j] < STALE_MS)
    return bid[j], ask[j], valid


class Trek:
    def __init__(self):
        self.db = DB(); self.conn = None; self.m = None; self.info = {}

    def connect(self):
        self.conn = rpyc.classic.connect(*BRIDGE); self.conn._config['sync_request_timeout'] = 120
        self.conn.execute('import MetaTrader5 as _m5'); self.m = self.conn.modules.MetaTrader5
        for code, n in SYMBOLS.items():
            self.m.symbol_select(n, True)
        time.sleep(1)
        for code, n in SYMBOLS.items():
            i = self.m.symbol_info(n)
            if i is None:
                raise RuntimeError(f'no symbol_info for {n}')
            self.info[code] = dict(val=i.trade_tick_value / i.trade_tick_size, vmin=i.volume_min,
                                   swap_bps_s=abs(i.swap_long) / 100 / 31536000 * 1e4 if i.swap_mode == 5 else 0.0)
        if self.db.get('started') is None:
            self.db.put('started', time.time())
        log.info('connected; START %s, rule k=%g T=%ds pre=%ds hold=%ds lat=%ds', dt.datetime.fromtimestamp(START, dt.timezone.utc), K, T, PRE, HOLD, LAT)

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

    def sigma(self, code, now_s):
        key = f'sigma:{code}'
        if self.db.get(key) and now_s - int(self.db.get(f'sigma_at:{code}', 0)) < SIGMA_EVERY:
            return float(self.db.get(key))
        t = self.ticks(SYMBOLS[code], now_s - 86400, now_s)
        if t is None:
            raise RuntimeError(f'no ticks for sigma {code}')
        b, a, v = grid(*t, now_s - 86400, now_s - 6)
        lm = np.log((a + b) / 2); r = np.diff(lm); r[~(v[1:] & v[:-1])] = 0
        nz = r[r != 0]; s = float(np.median(np.abs(nz)) * 1.4826)
        self.db.put(key, s); self.db.put(f'sigma_at:{code}', now_s)
        log.info('%s sigma %.3e (%.3f bp) from %d nonzero 1 s returns', code, s, s * 1e4, len(nz))
        return s

    def process(self, code, now_s):
        G = now_s - 6                                           # last complete second we trust
        done = self.db.get(f'done:{code}')
        if done is None:                                         # first run: begin from now (no backfill)
            self.db.put(f'done:{code}', G - LAT - HOLD); return
        done = int(done); busy = int(self.db.get(f'busy:{code}', 0))
        te_max = min(G - LAT - HOLD, done + CHUNK)
        if te_max <= done:
            return
        sig = self.sigma(code, now_s)
        a_s = done - T - PRE - 5
        tk = self.ticks(SYMBOLS[code], a_s - 10, G + 1)
        if tk is None:
            log.warning('%s no ticks %d..%d', code, a_s, G); return
        bid, ask, valid = grid(*tk, a_s, G)
        mid = (bid + ask) / 2; lm = np.log(mid)
        r = np.r_[0.0, np.diff(lm)]; r[~valid] = 0; r[np.r_[False, ~valid[:-1]]] = 0
        rj = np.where(np.abs(r) > K * sig, 0.0, r); Lj = np.cumsum(rj)
        lo, hi = done - T + 1 - a_s, te_max - T - a_s           # spike index range whose event time is in (done, te_max]
        sp = np.flatnonzero(np.abs(r) > K * sig); sp = sp[(sp >= lo) & (sp <= hi) & (sp >= PRE)]
        info = self.info[code]; cnt = {}
        for i in sp:
            s_abs = int(a_s + i); te = s_abs + T
            day = dt.datetime.fromtimestamp(te, dt.timezone.utc).strftime('%Y-%m-%d')
            c = cnt.setdefault(day, dict(spikes=0, flips=0, taken=0, busy=0, invalid=0)); c['spikes'] += 1
            pre = np.sign(Lj[i] - Lj[i - PRE]); post = np.sign(Lj[i + T] - Lj[i])
            if pre == 0 or post == 0 or pre == post:
                continue
            c['flips'] += 1
            side = -int(post); e = te + LAT; x = e + HOLD
            if e <= busy:
                c['busy'] += 1; continue
            ei, xi = e - a_s, x - a_s
            if xi >= len(mid) or not valid[ei] or not valid[xi]:
                c['invalid'] += 1; continue
            pe = ask[ei] if side > 0 else bid[ei]; px = bid[xi] if side > 0 else ask[xi]
            net = side * (px - pe) / pe * 1e4 - info['swap_bps_s'] * HOLD
            gross = side * (mid[xi] - mid[ei]) / mid[ei] * 1e4
            pnl = side * (px - pe) * info['vmin'] * info['val']
            self.db.c.execute('''insert into trades(code, spike_s, event_s, side, entry_s, exit_s, entry, exit, entry_spread, exit_spread,
                net_bps, gross_bps, pnl_minlot, sigma, warmup) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (code, s_abs, te, side, e, x, float(pe), float(px), float(ask[ei] - bid[ei]), float(ask[xi] - bid[xi]),
                               float(net), float(gross), float(pnl), sig, int(te < START)))
            c['taken'] += 1; busy = x
            log.info('%s %s %s @ %.2f -> %.2f  %+.3f bp  $%+.3f%s', code, dt.datetime.fromtimestamp(e, dt.timezone.utc).strftime('%H:%M:%S'),
                     'BUY ' if side > 0 else 'SELL', pe, px, net, pnl, ' (warm-up)' if te < START else '')
        for day, c in cnt.items():
            self.db.count(day, code, **c)
        self.db.put(f'done:{code}', te_max); self.db.put(f'busy:{code}', busy); self.db.put(f'last_run:{code}', now_s)

    def run(self):
        while True:
            try:
                if os.path.exists(os.path.join(DATA, 'KILL_TREK')):
                    time.sleep(30); continue
                if self.conn is None:
                    self.connect()
                now_s = int(time.time())
                for code in SYMBOLS:
                    self.process(code, now_s)
            except Exception:
                log.error('loop error\n%s', traceback.format_exc()); self.conn = None; time.sleep(15)
            time.sleep(20)


if __name__ == '__main__':
    Trek().run()
