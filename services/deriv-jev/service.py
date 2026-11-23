"""deriv-jev: five Jev-gated DSI strategies on Deriv cTrader account 48223369.

ORDER_MODE=shadow (the only mode implemented): live M1 bars, live Jev decisions, fills simulated at the
next M1 open with the measured spread (identical to the backtest). The cTrader connection is market-data
only; no order is ever sent. Kill switch: create data/KILL to stop new Jev-approved entries.
"""
import os, sys, json, time, sqlite3, threading, logging, traceback
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ct
from engine import Track, resample, atr
from rules import generic_rules
from jev import ask, ask_median, state_text, state_v1
import ui

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data'); os.makedirs(DATA, exist_ok=True)
CFG = json.load(open(os.path.join(BASE, 'config.json')))
ENV = ct.load_env(os.path.join(BASE, '.env'))
ORDER_MODE = ENV.get('ORDER_MODE', 'shadow')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('deriv-jev')
HIST_MIN = 7 * 1440

STATUS = dict(started=time.time(), mode=ORDER_MODE, account=int(ENV['CTRADER_ACCOUNT_ID']), jev_model=CFG['jev_model'], jev_state=CFG.get('jev_state', 'base'), jev_asks=int(CFG.get('jev_asks', 1)),
              connected=False, last_poll=None, last_error=None, errors=0, symbols={}, phase='starting', kill=False,
              tracks={})
LOCK = threading.Lock()


def db():
    c = sqlite3.connect(os.path.join(DATA, 'deriv_jev.db'), timeout=30)
    c.execute('''create table if not exists signals(id integer primary key, ts real, tag text, code text, side int,
                 signal_t int, features text, state text, jev_p real, threshold real, latency real, decision text, error text)''')
    c.execute('''create table if not exists trades(id integer primary key, tag text, code text, side int, signal_t int,
                 entry_t int, entry_px real, risk real, exit_t int, exit_px real, r real, reason text, taken int,
                 jev_p real, signal_id int)''')
    c.execute('create index if not exists ix_tr on trades(tag, entry_t)')
    try:
        c.execute('alter table signals add column jev_ps text')   # all answers when Jev is asked several times
    except sqlite3.OperationalError:
        pass
    c.commit(); return c


class Runner:
    def __init__(self):
        self.con = db()
        self.tracks = {s['tag']: Track(s, s['spread_override'] if s.get('spread_override') is not None else CFG['symbols'][s['code']]['spread'],
                                       CFG['swap_per_min']) for s in CFG['strategies']}
        self.bars = {}       # code -> dict of arrays t,o,h,l,c
        self.cli = None

    # ---------- cTrader ----------
    def connect(self):
        self.cli = ct.Client(ENV.get('CTRADER_HOST', 'demo.ctraderapi.com')); self.cli.auth(ENV)
        STATUS['connected'] = True; log.info('cTrader connected (market data only)')
        self.check_modes()

    def check_modes(self):
        ids = {v['id']: k for k, v in CFG['symbols'].items()}
        names = {0: 'ENABLED', 1: 'DISABLED_WITHOUT_PENDINGS', 2: 'DISABLED_WITH_PENDINGS', 3: 'CLOSE_ONLY'}
        for s in self.cli.symbol_specs(list(ids)):
            STATUS['symbols'].setdefault(ids[s.symbolId], {})['trading_mode'] = names.get(s.tradingMode, str(s.tradingMode))
        STATUS['modes_checked'] = time.time()

    def fetch(self, code, t0_min, t1_min):
        sid = CFG['symbols'][code]['id']; rows = []; a = t0_min
        while a < t1_min:
            b = min(t1_min, a + 7 * 1440)
            r, more = self.cli.m1(sid, a * 60000, b * 60000)
            rows += r; time.sleep(0.3)
            if more and r:
                a = max(x[0] for x in r) + 1
            else:
                a = b
        rows = sorted({x[0]: x for x in rows}.values())
        return np.array(rows, float) if rows else np.zeros((0, 6))

    # ---------- processing ----------
    def strategies_for(self, code):
        return [s for s in CFG['strategies'] if s['code'] == code]

    def process(self, code, new_idx, live):
        """Feed M1 bars new_idx (indices into self.bars[code]) through the tracks of this symbol."""
        B = self.bars[code]
        strats = self.strategies_for(code)
        cache = {}
        for j in new_idx:
            t, o, h, l, c = int(B['t'][j]), B['o'][j], B['h'][j], B['l'][j], B['c'][j]
            for s in strats:
                for kind, P in self.tracks[s['tag']].on_m1(t, o, h, l, c):
                    self.on_event(s, kind, P, live)
            # tf bars completing with this M1 bar
            for s in strats:
                tf = s['tf']
                if (t + 1) % tf != 0:
                    continue
                if live or tf not in cache:
                    upto = t + 1 if live else int(B['t'][-1]) + 1
                    lo = 0 if not live else max(0, j - HIST_MIN)
                    b = resample(B['t'][lo:j + 1] if live else B['t'], B['o'][lo:j + 1] if live else B['o'],
                                 B['h'][lo:j + 1] if live else B['h'], B['l'][lo:j + 1] if live else B['l'],
                                 B['c'][lo:j + 1] if live else B['c'], tf, upto)
                    a = atr(b); rr = generic_rules(b, a)
                    cache[tf] = (b, a, rr)
                b, a, rr = cache[tf]
                if live:
                    k = len(b['t']) - 1
                else:
                    k = int(np.searchsorted(b['t'], t + 1 - tf))
                    if k >= len(b['t']) or b['t'][k] != t + 1 - tf:
                        continue
                if k < 0 or b['t'][k] + tf != t + 1:
                    continue
                P = self.tracks[s['tag']].on_bar(b, a, rr[s['rule']], k)
                if P is not None:
                    self.on_signal(s, P, live)
            if live:
                cache = {}

    def on_signal(self, s, P, live):
        if not live:
            P['taken'] = False; P['decision'] = 'replay'; return
        if s.get('group') == 'wr70':        # 70%+ win-rate group: no Jev gate, every signal is taken (as backtested)
            kill = os.path.exists(os.path.join(DATA, 'KILL')); dec = 'killed' if kill else 'take'
            P['taken'] = dec == 'take'; P['jev_p'] = None; P['decision'] = dec
            with LOCK:
                cur = self.con.execute('insert into signals(ts,tag,code,side,signal_t,features,state,jev_p,threshold,latency,decision,error,jev_ps) values(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                       (time.time(), s['tag'], s['code'], P['side'], P['signal_t'], json.dumps(P['F']), None, None, None, None, dec, 'no gate', None))
                self.con.commit(); P['signal_id'] = cur.lastrowid
            return
        if CFG.get('jev_state') == 'v1-history':
            st = state_v1(s['code'], P['F'], P['side'], P['hist'], P['moves'], P['signal_t'])
        else:
            st = state_text(s['code'], P['F'], P['side'])
        n = int(CFG.get('jev_asks', 1))
        if n > 1:
            p, ps, lat, err = ask_median(ENV['JEV_API_KEY'], CFG['jev_model'], st, n)
        else:
            p, lat, err = ask(ENV['JEV_API_KEY'], CFG['jev_model'], st); ps = [p] if p is not None else []
        kill = os.path.exists(os.path.join(DATA, 'KILL')); STATUS['kill'] = kill
        if p is None:
            dec = 'jev_error'
        elif p >= s['jev_threshold']:
            dec = 'killed' if kill else 'take'
        else:
            dec = 'skip'
        P['taken'] = dec == 'take'; P['jev_p'] = p; P['decision'] = dec
        with LOCK:
            cur = self.con.execute('insert into signals(ts,tag,code,side,signal_t,features,state,jev_p,threshold,latency,decision,error,jev_ps) values(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (time.time(), s['tag'], s['code'], P['side'], P['signal_t'], json.dumps(P['F']), json.dumps(st), p, s['jev_threshold'], lat, dec, err, json.dumps(ps)))
            self.con.commit(); P['signal_id'] = cur.lastrowid
        log.info('%s signal %s jev_p=%s (answers %s) thr=%s -> %s %s', s['tag'], 'LONG' if P['side'] > 0 else 'SHORT', p, ps, s['jev_threshold'], dec, err or '')

    def on_event(self, s, kind, P, live):
        with LOCK:
            if kind == 'entry':
                row = self.con.execute('select id, taken, jev_p from trades where tag=? and entry_t=?', (s['tag'], P['entry_t'])).fetchone()
                if row:                      # restart: re-attach to the stored trade
                    P['db_id'], P['taken'], P['jev_p'] = row[0], bool(row[1]), row[2]
                elif live:
                    cur = self.con.execute('insert into trades(tag,code,side,signal_t,entry_t,entry_px,risk,taken,jev_p,signal_id) values(?,?,?,?,?,?,?,?,?,?)',
                                           (s['tag'], s['code'], P['side'], P['signal_t'], P['entry_t'], P['entry_px'], P['risk'], int(P.get('taken', False)), P.get('jev_p'), P.get('signal_id')))
                    P['db_id'] = cur.lastrowid
                    if P.get('taken'):
                        log.info('%s ENTRY (shadow) %s @ %.2f risk %.2f', s['tag'], 'LONG' if P['side'] > 0 else 'SHORT', P['entry_px'], P['risk'])
            elif kind == 'exit' and P.get('db_id'):
                self.con.execute('update trades set exit_t=?, exit_px=?, r=?, reason=? where id=?', (P['exit_t'], P['exit_px'], P['r'], P['reason'], P['db_id']))
                if P.get('taken') and live:
                    log.info('%s EXIT (shadow) %s r=%.2f', s['tag'], P['reason'], P['r'])
            self.con.commit()

    def snapshot(self):
        out = {}
        for tag, tr in self.tracks.items():
            code = tr.cfg['code']; last = float(self.bars[code]['c'][-1]) if code in self.bars and len(self.bars[code]['c']) else None
            pos = None
            if tr.pos is not None:
                P = tr.pos
                pos = dict(side=P['side'], entry_t=P['entry_t'], entry_px=P['entry_px'], sl=P['sl'], risk=P['risk'], bars=P['bars'],
                           taken=bool(P.get('taken')), jev_p=P.get('jev_p'), warmup=(not P.get('taken')) and P.get('jev_p') is None,
                           open_r=(P['side'] * (last - P['entry_px']) - tr.spread) / P['risk'] if last else None)
            out[tag] = dict(pos=pos, pending=bool(tr.pending), last=last)
        return out

    # ---------- main loop ----------
    def startup(self):
        STATUS['phase'] = 'loading history'
        now = int(time.time() // 60)
        for code in CFG['symbols']:
            A = self.fetch(code, now - HIST_MIN, now)
            A = A[A[:, 0] < now]
            self.bars[code] = dict(t=A[:, 0].astype(np.int64), o=A[:, 1], h=A[:, 2], l=A[:, 3], c=A[:, 4])
            log.info('%s history %d bars', code, len(A))
        STATUS['phase'] = 'replaying history'
        for code in CFG['symbols']:
            self.process(code, range(len(self.bars[code]['t'])), live=False)
        # stored open trades that the replay did not re-attach (e.g. long downtime)
        with LOCK:
            for tag, tr in self.tracks.items():
                open_ids = [r[0] for r in self.con.execute('select id from trades where tag=? and exit_t is null', (tag,))]
                keep = tr.pos.get('db_id') if tr.pos else None
                for i in open_ids:
                    if i != keep:
                        self.con.execute("update trades set reason='restart-unmatched' where id=?", (i,))
            self.con.commit()
        STATUS['phase'] = 'live'
        log.info('replay done; live')

    def poll(self):
        now = int(time.time() // 60)
        for code in CFG['symbols']:
            B = self.bars[code]; last = int(B['t'][-1])
            A = self.fetch(code, last - 3, now)
            A = A[(A[:, 0] > last) & (A[:, 0] < now)] if len(A) else A
            if len(A):
                n0 = len(B['t'])
                for k, col in zip('tohlc', range(5)):
                    B[k] = np.r_[B[k], A[:, col].astype(np.int64) if k == 't' else A[:, col]]
                cut = max(0, len(B['t']) - HIST_MIN - 500)
                if cut:
                    for k in B: B[k] = B[k][cut:]
                    n0 -= cut
                self.process(code, range(n0, len(B['t'])), live=True)
            STATUS['symbols'].setdefault(code, {}).update(last_bar=int(B['t'][-1]), last=float(B['c'][-1]))
        STATUS['last_poll'] = time.time()
        if time.time() - STATUS.get('modes_checked', 0) > 3600:
            self.check_modes()

    def run(self):
        backoff = 5
        while True:
            try:
                if self.cli is None:
                    self.connect()
                if STATUS['phase'] != 'live':
                    self.startup()
                with LOCK:
                    STATUS['tracks'] = self.snapshot()
                t = time.time(); wait = 60 - (t % 60) + 4
                while wait > 0:
                    time.sleep(min(wait, 8)); wait -= 8
                    self.cli.heartbeat()
                self.poll()
                with LOCK:
                    STATUS['tracks'] = self.snapshot()
                backoff = 5
            except Exception as e:
                STATUS['errors'] += 1; STATUS['last_error'] = f'{type(e).__name__}: {e}'[:300]; STATUS['connected'] = False
                log.error('loop error: %s\n%s', e, traceback.format_exc())
                try:
                    self.cli.s.close()
                except Exception:
                    pass
                self.cli = None; time.sleep(backoff); backoff = min(backoff * 2, 300)


if __name__ == '__main__':
    if ORDER_MODE != 'shadow':
        log.error('ORDER_MODE=%s is not implemented; only shadow is. Exiting.', ORDER_MODE); sys.exit(2)
    R = Runner()
    ui.start(ENV, STATUS, LOCK, os.path.join(DATA, 'deriv_jev.db'), CFG)
    R.run()
