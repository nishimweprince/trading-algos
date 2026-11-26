"""Read-only monitor of the IC runner's break-even actions, shared by the IC dashboard (ic_live_ui.py, /breakeven)
and the Deriv paper dashboard (services/deriv-jev/deriv_paper_ui.py, /breakeven).

Sources: the runner's log (stop moves), its orders table (market closes and each trade's R/ATR), and broker deals read
through the execution bridge without initialising or shutting down the runner's session. For every event the trigger
tick (first exit-side tick at 4 ATR in favour after the fill) is found once from IC ticks, so the old once-a-minute
events and the tick-level ones have the same lag measure."""
import datetime as dt, html, logging, os, re, sqlite3, threading, time
import rpyc
IC_DIR = os.path.dirname(os.path.abspath(__file__))
IC_LOG, IC_DB = os.path.join(IC_DIR, 'logs', 'ic_m8.log'), os.path.join(IC_DIR, 'data', 'ic_m8.db')
IC_SYM = {709001: 'Boom1000', 709002: 'Boom1000', 709003: 'Boom1000', 709004: 'Jump50'}
IC_TAG_SYM = {'IC-B1000-M5-RS': 'Boom1000', 'IC-B1000-M5-TP1': 'Boom1000', 'IC-B1000-M5-TP2': 'Boom1000', 'IC-J50-M1-RS': 'Jump50'}
BE_TICK_SINCE = dt.datetime(2026, 11, 26, 11, 2, 5, tzinfo=dt.timezone.utc).timestamp()   # tick-level watcher deployed
BE_ATR = 4.0
RX_NEW = re.compile(r'^(\S+ \S+),\d+ INFO (\S+) breakeven \(tick (\d+), [\d.]+s ago\) (\d+) sl ([\d.]+) -> ([\d.]+): (\S+)')
RX_OLD = re.compile(r'^(\S+ \S+),\d+ INFO (\S+) breakeven (\d+) sl ([\d.]+) -> ([\d.]+) \((\S+)\)')
ic = {'deals': [], 'off': 7200, 'trig': {}, 'error': 'Starting', 'at': None}


def be_events():
    """Break-even actions from the runner's log (stop moves) and orders table (market closes). Times are host-local
    (Europe/Berlin) log stamps converted to epoch seconds."""
    ev = []
    try:
        with open(IC_LOG, errors='replace') as f:
            for line in f:
                if 'breakeven' not in line:
                    continue
                m = RX_NEW.match(line)
                if m:
                    ev.append(dict(t=time.mktime(time.strptime(m[1], '%Y-%m-%d %H:%M:%S')), tag=m[2], ticket=int(m[4]),
                                   kind='Stop moved to entry' if m[7] == '10009' else f'Stop change rejected ({m[7]})', mode='tick'))
                    continue
                m = RX_OLD.match(line)
                if m:
                    ev.append(dict(t=time.mktime(time.strptime(m[1], '%Y-%m-%d %H:%M:%S')), tag=m[2], ticket=int(m[3]),
                                   kind='Stop moved to entry' if m[6] == '10009' else f'Stop change rejected ({m[6]})', mode='minute'))
    except FileNotFoundError:
        pass
    try:
        c = sqlite3.connect(f'file:{IC_DB}?mode=ro', uri=True, timeout=3)
        for ts, tag, ticket, rc in c.execute("select ts, tag, ticket, retcode from orders where kind='close' and note like 'breakeven:%'"):
            ev.append(dict(t=ts, tag=tag, ticket=int(ticket or 0), kind='Closed at market (price back through entry)' if rc == 10009 else f'Market close failed ({rc})', mode='tick'))
        risk = {int(t): (R, a) for t, R, a in c.execute("select ticket, R, atr from orders where kind='open' and ticket is not null and retcode=10009")}
        c.close()
    except Exception:
        risk = {}
    for e in ev:
        if e['mode'] == 'tick' and e['t'] < BE_TICK_SINCE:
            e['mode'] = 'minute'
    return sorted(ev, key=lambda e: e['t']), risk


def poll():
    """Every 60 s: broker deals (read-only; the runner owns the session) and, once per event, the trigger tick:
    the first exit-side tick at 4 ATR in favour after the fill."""
    while True:
        try:
            ev, risk = be_events()
            with rpyc.classic.connect('127.0.0.1', 18814) as conn:
                conn._config['sync_request_timeout'] = 30
                m5 = conn.modules.MetaTrader5; conn.execute('import MetaTrader5 as _m5')
                tk = m5.symbol_info_tick('Jump50')
                off = int(round((tk.time_msc / 1000.0 - time.time()) / 3600.0) * 3600) if tk and tk.time_msc else ic['off']
                srv_now = int(time.time()) + off
                deals = m5.history_deals_get(srv_now - 30 * 86400, srv_now + 60)
                D = rpyc.classic.obtain(conn.eval('lambda rows: [dict(r._asdict()) for r in rows]')(deals)) if deals is not None else []
                opens = {d['position_id']: d for d in D if d['entry'] == 0}
                for e in ev:
                    tkt = e['ticket']
                    if tkt in ic['trig'] or tkt not in opens or tkt not in risk:
                        continue
                    o = opens[tkt]; side = 1 if o['type'] == 0 else -1; atr = risk[tkt][1]
                    a_s = int(o['time_msc'] // 1000) - 1; b_s = int(e['t']) + off + 5
                    conn.execute(f"_tk = _m5.copy_ticks_range({IC_SYM.get(o['magic'], 'Jump50')!r}, {a_s}, {b_s}, _m5.COPY_TICKS_ALL)\n"
                                 "_x = None if _tk is None or len(_tk) == 0 else (_tk['time_msc'].tolist(), _tk['bid'].tolist(), _tk['ask'].tolist())")
                    x = rpyc.classic.obtain(conn.eval('_x'))
                    hit = None
                    if x:
                        for ms, b, a in zip(*x):
                            if ms > o['time_msc'] and side * ((b if side > 0 else a) - o['price']) >= BE_ATR * atr:
                                hit = ms; break
                    ic['trig'][tkt] = hit
            ic.update(deals=D, off=off, error=None, at=time.time())
        except Exception:
            logging.exception('IC break-even data unavailable'); ic['error'] = 'IC broker data unavailable'
        time.sleep(60)


def render(table):
    """Returns (health line, body HTML). `table(headers, rows, empty)` is the caller's table helper."""
    ev, risk = be_events(); off = ic['off']; D = ic['deals']
    opens = {d['position_id']: d for d in D if d['entry'] == 0}
    closes = {}
    for d in D:
        if d['entry'] in (1, 3) and d['position_id']:
            closes.setdefault(d['position_id'], []).append(d)
    def lag(e):
        ms = ic['trig'].get(e['ticket'])
        return e['t'] - (ms / 1000.0 - off) if ms else None
    rows, outcome = [], {}
    for e in ev:
        L = lag(e); e['lag'] = L
        o = opens.get(e['ticket']); cl = closes.get(e['ticket'])
        res = '—'
        if o and cl and e['ticket'] in risk:
            side = 1 if o['type'] == 0 else -1; R = risk[e['ticket']][0]
            r = side * (cl[-1]['price'] - o['price']) / R; usd = sum(c['profit'] + c['swap'] + c['commission'] + c['fee'] for c in cl)
            res = f'{r:+.2f}R (${usd:+.2f})'; outcome[e['ticket']] = (e['mode'], r)
        elif o and not cl:
            res = 'still open'
        rows.append((dt.datetime.fromtimestamp(e['t'], dt.timezone.utc).strftime('%b %d %H:%M:%S'), e['tag'], e['ticket'], e['kind'],
                     'every tick' if e['mode'] == 'tick' else 'once a minute', f'{L:.1f} s' if L is not None else '—', res))
    def card(mode, title, note):
        E = [e for e in ev if e['mode'] == mode]; lags = sorted(e['lag'] for e in E if e['lag'] is not None)
        O = [r for m, r in outcome.values() if m == mode]
        med = lags[len(lags) // 2] if lags else None
        items = [('Events', f'{len(E)}'), ('Median lag', f'{med:.1f} s' if med is not None else '—'),
                 ('Worst lag', f'{lags[-1]:.1f} s' if lags else '—'),
                 ('Closed at market', f"{sum(1 for e in E if e['kind'].startswith('Closed'))}"),
                 ('Ended at break-even', f'{sum(1 for r in O if abs(r) < 0.05)}/{len(O)}' if O else '—'),
                 ('Avg result', f'{sum(O) / len(O):+.2f}R' if O else '—')]
        return (f'<section class="summary"><h2>{html.escape(title)}</h2><p class="muted">{html.escape(note)}</p><dl>'
                + ''.join(f'<div><dt>{a}</dt><dd>{b}</dd></div>' for a, b in items) + '</dl></section>')
    since = dt.datetime.fromtimestamp(BE_TICK_SINCE, dt.timezone.utc)
    cards = card('tick', 'Tick-level (current)', f'Since {since:%b %d %H:%M} UTC') + card('minute', 'Once a minute (before)', f'Before {since:%b %d %H:%M} UTC')
    health = ic['error'] or (f"IC broker data updated {time.time() - ic['at']:.0f} s ago" if ic['at'] else 'Loading IC broker data')
    body = (f'<section class="lead"><h2>IC Markets live break-even</h2><p>The live IC runner moves a position\'s stop to its entry price once '
            f'the price has moved 4 ATR in its favour. Until {since:%b %d %H:%M} UTC that check ran once a minute; it now reads every tick and '
            f'acts within about a second. If the price has already come back through entry, the position is closed at market instead.</p>'
            f'<p class="muted">Lag is the time from the first tick at 4 ATR in favour to the stop change, measured the same way for both '
            f'periods. Results are from the broker\'s closing deals. This tab is live trading, not paper.</p></section>'
            f'<div class="summaries">{cards}</div>'
            f'<section><h2>Break-even events</h2><p class="muted">Newest first.</p>'
            + table(['Time (UTC)', 'Strategy', 'Position', 'Action', 'Checked', 'Lag', 'Trade result'], rows[::-1][:80], 'No break-even events yet.') + '</section>')
    return health + ' · read-only view of the live IC runner', body



_started = False


def start():
    """Start the background poller once per process."""
    global _started
    if not _started:
        _started = True
        threading.Thread(target=poll, daemon=True).start()
