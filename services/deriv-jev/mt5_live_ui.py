"""Read-only monitor for the Deriv MT5 runners."""
import datetime as dt
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import rpyc

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / 'config.json').read_text())
GROUPS = [('wr70', 707001, 'mt5-runner', 'KILL_MT5'),
          ('jev', 708001, 'mt5-jev', 'KILL_JEV')]
STRATEGIES = {
    magic: (group, spec['tag'])
    for group, start, _, _ in GROUPS
    for magic, spec in enumerate((s for s in CONFIG['strategies'] if s.get('group') == group), start)
}
POLL_SECONDS = 10
HISTORY_DAYS = 7
latest = {'error': 'Waiting for MT5', 'broker': None, 'runners': []}


def broker_snapshot():
    env = {}
    path = BASE / '.env.mt5'
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and not key.lstrip().startswith('#'):
                env[key.strip()] = value.strip()
    bridge = env.get('BRIDGE_HOST', '127.0.0.1'), int(env.get('BRIDGE_PORT', '18812'))
    now = dt.datetime.now(dt.timezone.utc)
    with rpyc.classic.connect(*bridge) as conn:
        conn._config['sync_request_timeout'] = 8
        mt5 = conn.modules.MetaTrader5
        # The trading runners own the terminal session. Query it without initializing or shutting it down.
        account = mt5.account_info()
        positions = mt5.positions_get()
        deals = mt5.history_deals_get(int(now.timestamp()) - HISTORY_DAYS * 86400,
                                     int(now.timestamp()) + 1)
        if account is None or positions is None or deals is None:
            raise RuntimeError('MT5 account, positions or deals unavailable')
        currency = str(account.currency)
        # Convert the native MT5 named tuples on the bridge before transferring them.
        records = conn.eval('lambda rows: [dict(row._asdict()) for row in rows]')
        positions = [SimpleNamespace(**row) for row in rpyc.classic.obtain(records(positions))]
        deals = [SimpleNamespace(**row) for row in rpyc.classic.obtain(records(deals))]
    open_positions = [
        dict(strategy=STRATEGIES[p.magic][1], group=STRATEGIES[p.magic][0], symbol=p.symbol, side='Buy' if p.type == 0 else 'Sell',
             lots=p.volume, entry=p.price_open, price=p.price_current, stop=p.sl,
             target=p.tp, pnl=p.profit+p.swap)
        for p in positions if p.magic in STRATEGIES
    ]
    # Broker-generated exits can have a different magic. Attribute them via the position.
    position_magic = {d.position_id: d.magic for d in deals if d.magic in STRATEGIES and d.position_id}
    position_magic.update({p.identifier: p.magic for p in positions if p.magic in STRATEGIES})
    executions = [
        dict(time=d.time, strategy=STRATEGIES[position_magic[d.position_id]][1],
             group=STRATEGIES[position_magic[d.position_id]][0], symbol=d.symbol,
             entry={0: 'Open', 1: 'Close', 2: 'Reverse', 3: 'Close by'}.get(d.entry, 'Other'),
             side='Buy' if d.type == 0 else 'Sell', price=d.price, lots=d.volume,
             pnl=d.profit+d.swap+d.commission+d.fee)
        for d in deals if d.position_id in position_magic and d.type in (0, 1)
    ]
    executions.sort(key=lambda d: d['time'], reverse=True)
    pnl_by_group = {
        group: dict(realized=sum(d['pnl'] for d in executions if d['group'] == group),
                    floating=sum(p['pnl'] for p in open_positions if p['group'] == group))
        for group, _, _, _ in GROUPS
    }
    return dict(pnl_by_group=pnl_by_group, as_of=now.isoformat(), currency=currency, positions=open_positions,
                deals=executions[:100], deal_count=len(executions),
                realized=sum(d['pnl'] for d in executions),
                floating=sum(p['pnl'] for p in open_positions))


def monitor():
    global latest
    while True:
        runners = []
        for group, _, unit, kill in GROUPS:
            try:
                status = subprocess.run(['systemctl', 'is-active', unit], capture_output=True,
                                        text=True, timeout=3).stdout.strip() or 'unknown'
            except (OSError, subprocess.TimeoutExpired):
                status = 'unknown'
            runners.append(dict(group=group.upper(), status=status, paused=(BASE / 'data' / kill).exists()))
        try:
            latest = dict(broker=broker_snapshot(), runners=runners, error=None)
        except Exception:
            logging.exception('Unable to read MT5 snapshot')
            latest = dict(broker=latest['broker'], runners=runners, error='MT5 data unavailable')
        time.sleep(POLL_SECONDS)


def table(headers, rows, empty):
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    labels = [html.escape(label, quote=True) for label in headers]
    cells = ''.join(
        '<tr>' + ''.join(f'<td data-label="{label}">{html.escape(str(value))}</td>'
                         for label, value in zip(labels, row)) + '</tr>'
        for row in rows
    )
    headings = ''.join(f'<th scope="col">{label}</th>' for label in labels)
    return f'<table><thead><tr>{headings}</tr></thead><tbody>{cells}</tbody></table>'


def group_summary(broker, runners):
    statuses = {runner['group']: runner for runner in runners}
    currency = html.escape(broker['currency']) if broker else ''
    groups = []
    for key, label in [('wr70', 'WR70'), ('jev', 'Jev')]:
        runner = statuses.get(key.upper())
        status = 'Waiting for runner status'
        if runner:
            status = ('Entries paused' if runner['paused'] else 'Runner active') if runner['status'] == 'active' else f"Runner {runner['status']}"
        totals = broker['pnl_by_group'][key] if broker else None
        amounts = ''
        for metric, description in [('realized', 'Net deal P&L · last 7 days'), ('floating', 'Open P&L')]:
            value = f'{totals[metric]:+,.2f} {currency}' if totals else 'Unavailable'
            amounts += f'<div><dt>{html.escape(description)}</dt><dd>{value}</dd></div>'
        groups.append(f'<section class="summary"><h2>{label}</h2><p class="muted">{html.escape(status)}</p>'
                      f'<dl>{amounts}</dl></section>')
    return '<div class="summaries">' + ''.join(groups) + '</div>'


def page(state):
    broker = state['broker']
    health = state['error'] or 'MT5 connected'
    if broker:
        stamp = dt.datetime.fromisoformat(broker['as_of']).strftime('%b %d, %H:%M:%S UTC')
        health += f' · {"Last successful update" if state["error"] else "Updated"} {stamp}'
    sections = group_summary(broker, state['runners'])
    if broker:
        currency = broker['currency']
        positions = table(['Strategy', 'Symbol', 'Side', 'Lots', 'Entry', 'Current', 'Stop', 'Target', f'P&L ({currency})'],
                          [(p['strategy'], p['symbol'], p['side'], f"{p['lots']:g}", f"{p['entry']:.2f}",
                            f"{p['price']:.2f}", f"{p['stop']:.2f}" if p['stop'] else '—',
                            f"{p['target']:.2f}" if p['target'] else '—', f"{p['pnl']:+,.2f}")
                           for p in broker['positions']], 'No open WR70 or Jev positions.')
        deals = table(['Time (UTC)', 'Strategy', 'Symbol', 'Action', 'Side', 'Lots', 'Price', f'Net ({currency})'],
                      [(dt.datetime.fromtimestamp(d['time'], dt.timezone.utc).strftime('%b %d %H:%M:%S'),
                        d['strategy'], d['symbol'], d['entry'], d['side'], f"{d['lots']:g}",
                        f"{d['price']:.2f}", f"{d['pnl']:+,.2f}")
                       for d in broker['deals']], 'No WR70 or Jev executions in the last seven days.')
        sections += (f'<section><h2>Open positions</h2>{positions}</section>'
                     f'<section><h2>Broker executions</h2><p class="muted">Showing {len(broker["deals"])} of '
                     f'{broker["deal_count"]} deals from the last seven days. Net includes reported swap, '
                     f'commission, and fees.</p>{deals}</section>')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="{POLL_SECONDS}">
<title>Deriv MT5 Live</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&amp;display=swap">
<style>
:root {{ color-scheme:light dark; --bg:#fff; --surface:#f2f2f2; --text:#000; --muted:#595959;
  background:var(--bg); color:var(--text); font:13px/1.55 "DM Sans",system-ui,sans-serif; }}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#000; --surface:#141414; --text:#fff; --muted:#aaa; }}
}}
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
dl {{ display:flex; gap:12px 24px; flex-wrap:wrap; margin-top:14px; }}
dd {{ font-size:18px; font-weight:600; letter-spacing:-.035em; font-variant-numeric:tabular-nums; }}
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
@media (max-width:360px) {{ .summaries {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header><h1>Deriv MT5 Live</h1><p class="muted">{html.escape(health)}</p></header>
<main>{sections}</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = latest
        if self.path == '/':
            body = page(state).encode()
            content_type = 'text/html; charset=utf-8'
        elif self.path == '/api/state':
            body = json.dumps(state).encode()
            content_type = 'application/json'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    with ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('MT5_UI_PORT', '8092'))), Handler) as server:
        threading.Thread(target=monitor, daemon=True).start()
        server.serve_forever()
