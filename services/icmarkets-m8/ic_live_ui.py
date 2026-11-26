"""Read-only monitor for the IC Markets runner (ic-m8): account, per-strategy P&L, open positions, broker deals,
and the runner's own signal/order log. Serves http://127.0.0.1:IC_UI_PORT (default 8093)."""
import datetime as dt
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import rpyc

import be_monitor

BASE = Path(__file__).resolve().parent
STRATEGIES = {709001: 'Boom1000 M5 · reverse + stop', 709002: 'Boom1000 M5 · TP 1R',
              709003: 'Boom1000 M5 · TP 2R', 709004: 'Jump50 M1 · reverse + stop'}
TAGS = {'IC-B1000-M5-RS': 709001, 'IC-B1000-M5-TP1': 709002, 'IC-B1000-M5-TP2': 709003, 'IC-J50-M1-RS': 709004}
POLL_SECONDS = 10
HISTORY_DAYS = 30
latest = {'error': 'Waiting for MT5', 'broker': None, 'runner': {}, 'log': {}}


def env():
    out = {}
    p = BASE / '.env'
    if p.exists():
        for line in p.read_text().splitlines():
            k, sep, v = line.partition('=')
            if sep and not k.lstrip().startswith('#'):
                out[k.strip()] = v.strip()
    return out


def enabled(e):
    return [m for tag, m in TAGS.items() if e.get(f"IC_ENABLE_{tag.replace('-', '_')}", '1') == '1']


def broker_snapshot(e):
    with rpyc.classic.connect('127.0.0.1', int(e.get('IC_PORT', '18814'))) as conn:
        conn._config['sync_request_timeout'] = 8
        mt5 = conn.modules.MetaTrader5
        # The runner owns the terminal session: read it without initializing or shutting it down.
        account = mt5.account_info()
        tick = mt5.symbol_info_tick('Jump50') or mt5.symbol_info_tick('Boom1000')
        off = int(round((tick.time_msc / 1000.0 - time.time()) / 3600.0) * 3600) if tick and tick.time_msc else 7200
        srv_now = int(time.time()) + off                      # IC labels times in server time (UTC+2/+3)
        positions = mt5.positions_get()
        deals = mt5.history_deals_get(srv_now - HISTORY_DAYS * 86400, srv_now + 60)
        if account is None or positions is None or deals is None:
            raise RuntimeError('MT5 account, positions or deals unavailable')
        records = conn.eval('lambda rows: [dict(row._asdict()) for row in rows]')
        positions = [SimpleNamespace(**r) for r in rpyc.classic.obtain(records(positions))]
        deals = [SimpleNamespace(**r) for r in rpyc.classic.obtain(records(deals))]
        acc = SimpleNamespace(**rpyc.classic.obtain(conn.eval('lambda a: dict(a._asdict())')(account)))
    pos = [dict(magic=p.magic, strategy=STRATEGIES[p.magic], symbol=p.symbol, side='Buy' if p.type == 0 else 'Sell', lots=p.volume,
                entry=p.price_open, price=p.price_current, stop=p.sl, target=p.tp, pnl=p.profit + p.swap,
                opened=p.time - off)
           for p in positions if p.magic in STRATEGIES]
    pmagic = {d.position_id: d.magic for d in deals if d.magic in STRATEGIES and d.position_id}
    pmagic.update({p.identifier: p.magic for p in positions if p.magic in STRATEGIES})
    ex = [dict(time=d.time - off, magic=pmagic[d.position_id], strategy=STRATEGIES[pmagic[d.position_id]], symbol=d.symbol,
               entry={0: 'Open', 1: 'Close', 2: 'Reverse', 3: 'Close by'}.get(d.entry, 'Other'),
               side='Buy' if d.type == 0 else 'Sell', price=d.price, lots=d.volume,
               pnl=d.profit + d.swap + d.commission + d.fee, reason=d.comment)
          for d in deals if d.position_id in pmagic and d.type in (0, 1)]
    ex.sort(key=lambda d: d['time'], reverse=True)
    closes = [d for d in ex if d['entry'] != 'Open']
    by = {m: dict(realized=sum(d['pnl'] for d in ex if d['magic'] == m),
                  floating=sum(p['pnl'] for p in pos if p['magic'] == m),
                  trades=sum(1 for d in closes if d['magic'] == m),
                  wins=sum(1 for d in closes if d['magic'] == m and d['pnl'] > 0)) for m in STRATEGIES}
    return dict(as_of=dt.datetime.now(dt.timezone.utc).isoformat(), currency=acc.currency, login=acc.login, server=acc.server,
                balance=acc.balance, equity=acc.equity, margin=acc.margin, free=acc.margin_free, by=by, positions=pos,
                deals=ex[:100], deal_count=len(ex), realized=sum(d['pnl'] for d in ex), floating=sum(p['pnl'] for p in pos),
                offset_h=off // 3600)


def runner_log():
    p = BASE / 'data' / 'ic_m8.db'
    if not p.exists():
        return {}
    c = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=3)
    sig = c.execute('select ts, tag, side, close, atr, action from signals order by id desc limit 20').fetchall()
    orders = c.execute('select ts, mode, tag, kind, side, lots, price, sl, tp, retcode, retmsg, note from orders '
                       'where mode="live" order by id desc limit 20').fetchall()
    skips = c.execute('select ts, tag, note from orders where retmsg="skipped" order by id desc limit 5').fetchall()
    c.close()
    return dict(signals=sig, orders=orders, skips=skips)


def monitor():
    global latest
    while True:
        e = env()
        try:
            status = subprocess.run(['systemctl', 'is-active', 'ic-m8'], capture_output=True, text=True, timeout=3).stdout.strip() or 'unknown'
        except (OSError, subprocess.TimeoutExpired):
            status = 'unknown'
        runner = dict(status=status, mode=e.get('IC_MODE', 'shadow').upper(), paused=(BASE / 'data' / 'KILL_IC').exists(),
                      risk=e.get('IC_RISK_PCT', '5'), enabled=enabled(e))
        try:
            lg = runner_log()
        except Exception:
            logging.exception('runner log unavailable'); lg = latest.get('log', {})
        try:
            latest = dict(broker=broker_snapshot(e), runner=runner, log=lg, error=None)
        except Exception:
            logging.exception('Unable to read MT5 snapshot')
            latest = dict(broker=latest['broker'], runner=runner, log=lg, error='MT5 data unavailable')
        time.sleep(POLL_SECONDS)


def table(headers, rows, empty):
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    labels = [html.escape(h, quote=True) for h in headers]
    cells = ''.join('<tr>' + ''.join(f'<td data-label="{lab}">{html.escape(str(v))}</td>' for lab, v in zip(labels, row)) + '</tr>' for row in rows)
    heads = ''.join(f'<th scope="col">{lab}</th>' for lab in labels)
    return f'<table><thead><tr>{heads}</tr></thead><tbody>{cells}</tbody></table>'


utc = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%b %d %H:%M:%S')


def page(state):
    b = state['broker']; r = state['runner'] or {}; lg = state.get('log') or {}
    health = state['error'] or 'MT5 connected'
    if b:
        stamp = dt.datetime.fromisoformat(b['as_of']).strftime('%b %d, %H:%M:%S UTC')
        health += f' · {"Last successful update" if state["error"] else "Updated"} {stamp}'
    run = ('Entries paused' if r.get('paused') else f"Runner active · {r.get('mode', '?')} · {r.get('risk', '?')}% risk per trade") \
        if r.get('status') == 'active' else f"Runner {r.get('status', 'unknown')}"
    cur = html.escape(b['currency']) if b else ''
    acct = ''
    if b:
        items = [('Balance', f"{b['balance']:,.2f} {cur}"), ('Equity', f"{b['equity']:,.2f} {cur}"),
                 ('Free margin', f"{b['free']:,.2f} {cur}"), (f'Net deal P&L · {HISTORY_DAYS} days', f"{b['realized']:+,.2f} {cur}"),
                 ('Open P&L', f"{b['floating']:+,.2f} {cur}")]
        acct = ''.join(f'<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>' for k, v in items)
    sections = (f'<section class="summary wide"><h2>Account {html.escape(str(b["login"])) if b else ""}</h2>'
                f'<p class="muted">{html.escape(b["server"]) if b else ""} · {html.escape(run)}</p><dl>{acct}</dl></section>')
    cards = ''
    for m in (r.get('enabled') or []):
        t = b['by'][m] if b else None
        st = f"{t['trades']} closed · {t['wins']} won" if t else ''
        vals = ''.join(f'<div><dt>{k}</dt><dd>{(f"{t[f]:+,.2f} {cur}") if t else "Unavailable"}</dd></div>'
                       for k, f in (('Net deal P&L', 'realized'), ('Open P&L', 'floating')))
        cards += f'<section class="summary"><h2>{html.escape(STRATEGIES[m])}</h2><p class="muted">{html.escape(st)}</p><dl>{vals}</dl></section>'
    sections = f'<div class="summaries">{sections}{cards}</div>'
    if b:
        positions = table(['Strategy', 'Symbol', 'Side', 'Lots', 'Entry', 'Current', 'Stop', 'Target', f'P&L ({cur})'],
                          [(p['strategy'], p['symbol'], p['side'], f"{p['lots']:g}", f"{p['entry']:.3f}", f"{p['price']:.3f}",
                            f"{p['stop']:.3f}" if p['stop'] else '—', f"{p['target']:.3f}" if p['target'] else '—', f"{p['pnl']:+,.2f}")
                           for p in b['positions']], 'No open positions.')
        deals = table(['Time (UTC)', 'Strategy', 'Symbol', 'Action', 'Side', 'Lots', 'Price', f'Net ({cur})'],
                      [(utc(d['time']), d['strategy'], d['symbol'], d['entry'], d['side'], f"{d['lots']:g}", f"{d['price']:.3f}",
                        f"{d['pnl']:+,.2f}") for d in b['deals']], f'No executions in the last {HISTORY_DAYS} days.')
        sections += (f'<section><h2>Open positions</h2>{positions}</section>'
                     f'<section><h2>Broker executions</h2><p class="muted">Showing {len(b["deals"])} of {b["deal_count"]} deals from the last '
                     f'{HISTORY_DAYS} days. Net includes reported swap, commission and fees.</p>{deals}</section>')
    sig = table(['Time (UTC)', 'Strategy', 'Signal', 'Bar close', 'ATR', 'Action'],
                [(utc(s[0]), s[1], 'Long' if s[2] > 0 else 'Short', f'{s[3]:.3f}', f'{s[4]:.3f}', s[5]) for s in lg.get('signals', [])],
                'No signals yet.')
    orders = table(['Time (UTC)', 'Strategy', 'Kind', 'Side', 'Lots', 'Price', 'Stop', 'Target', 'Result', 'Note'],
                   [(utc(o[0]), o[2], o[3], 'Buy' if o[4] > 0 else 'Sell', f'{o[5]:g}' if o[5] is not None else '—',
                     f'{o[6]:.3f}' if o[6] else '—', f'{o[7]:.3f}' if o[7] else '—', f'{o[8]:.3f}' if o[8] else '—',
                     f'{o[9]} {o[10]}' if o[9] is not None else o[10], o[11] or '') for o in lg.get('orders', [])],
                   'No live orders sent yet.')
    skips = ''.join(f'<li>{html.escape(utc(s[0]))} · {html.escape(s[1])}: {html.escape(s[2] or "")}</li>' for s in lg.get('skips', []))
    sections += (f'<section><h2>Runner signals</h2><p class="muted">Latest 20 signals the runner acted on (shadow and live).</p>{sig}</section>'
                 f'<section><h2>Live orders sent</h2>{orders}'
                 + (f'<p class="muted" style="margin-top:10px">Recent skipped trades:</p><ul class="muted">{skips}</ul>' if skips else '') + '</section>')
    return wrap(health, sections, 'live')


def wrap(health, sections, active):
    tabs = ''.join(f'<a href="{href}"' + (' aria-current="page"' if key == active else '') + f'>{label}</a>'
                   for key, href, label in (('live', '/', 'Live overview'), ('be', '/breakeven', 'Break-even')))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="{POLL_SECONDS}">
<title>IC Markets Live</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&amp;display=swap">
<style>
:root {{ color-scheme:light dark; --bg:#fff; --surface:#f2f2f2; --text:#000; --muted:#595959;
  background:var(--bg); color:var(--text); font:13px/1.55 "DM Sans",system-ui,sans-serif; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#000; --surface:#141414; --text:#fff; --muted:#aaa; }} }}
::selection {{ background:var(--text); color:var(--bg); }}
* {{ box-sizing:border-box; }}
body {{ max-width:1120px; margin:auto; padding:clamp(18px,5vw,52px) clamp(14px,4vw,38px); }}
h1,h2,p,dl,dd {{ margin:0; }}
h1 {{ font-size:24px; line-height:1.2; font-weight:650; letter-spacing:-.045em; }}
h2 {{ font-size:14px; font-weight:650; letter-spacing:-.015em; }}
.muted,dt {{ color:var(--muted); font-size:12px; }}
header p, section > p {{ margin-top:5px; }}
main {{ display:grid; gap:30px; margin-top:24px; }}
.summaries {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.summary {{ background:var(--surface); padding:14px 16px; border-radius:8px; }}
.summary.wide {{ grid-column:1/-1; }}
dl {{ display:flex; gap:12px 24px; flex-wrap:wrap; margin-top:14px; }}
dd {{ font-size:18px; font-weight:600; letter-spacing:-.035em; font-variant-numeric:tabular-nums; }}
ul {{ margin:4px 0 0; padding-left:18px; }}
table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; font-variant-numeric:tabular-nums; }}
td,th {{ text-align:left; padding:7px 8px; overflow-wrap:anywhere; }}
td:first-child,th:first-child {{ padding-left:0; }}
td:last-child,th:last-child {{ text-align:right; padding-right:0; }}
th {{ color:var(--muted); font-size:11px; font-weight:600; }}
@media (max-width:760px) {{
  table,tbody {{ display:block; }}
  thead {{ position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }}
  tr {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,100px),1fr)); gap:5px 12px; padding:10px 0; }}
  td,td:first-child,td:last-child {{ display:block; min-width:0; padding:0; text-align:left; }}
  td::before {{ content:attr(data-label); display:block; color:var(--muted); font-size:11px; }}
  td[data-label="Strategy"] {{ grid-column:1/-1; font-weight:600; }}
}}
@media (max-width:420px) {{ .summaries {{ grid-template-columns:1fr; }} }}
nav {{ display:flex; gap:4px; margin-top:16px; border-bottom:1px solid var(--surface); flex-wrap:wrap; }}
nav a {{ color:var(--muted); text-decoration:none; padding:7px 12px; border-radius:6px 6px 0 0; font-weight:600; }}
nav a[aria-current="page"] {{ color:var(--text); background:var(--surface); }}
.lead {{ background:var(--surface); padding:14px 16px; border-radius:8px; max-width:78ch; }} .lead p + p {{ margin-top:8px; }}
</style>
</head>
<body>
<header><h1>IC Markets Live</h1><p class="muted">{html.escape(health)}</p><nav aria-label="Views">{tabs}</nav></header>
<main>{sections}</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = latest
        if self.path == '/':
            body = page(state).encode(); ctype = 'text/html; charset=utf-8'
        elif self.path == '/breakeven':
            h, sections = be_monitor.render(table)
            body = wrap(h, sections, 'be').encode(); ctype = 'text/html; charset=utf-8'
        elif self.path == '/api/state':
            body = json.dumps(state, default=str).encode(); ctype = 'application/json'
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    with ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('IC_UI_PORT', '8093'))), Handler) as server:
        threading.Thread(target=monitor, daemon=True).start()
        be_monitor.start()
        server.serve_forever()
