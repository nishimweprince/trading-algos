"""Read-only dashboard for the Deriv paper trader (data/paper.db). Serves 127.0.0.1:PAPER_UI_PORT (default 8092)."""
import datetime as dt, html, json, os, re, sqlite3, time, threading, logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import rpyc

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'data', 'paper.db')
SYMBOLS = {'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30'}
CFG = json.load(open(os.path.join(BASE, 'config.json')))
TAGS = {s['tag']: s for s in CFG['strategies'] if s.get('group') in ('wr70', 'jev')}
GLABEL = {'wr70': 'WR70 group (9 strategies)', 'jev': 'Former Jev group (5 strategies, no Jev filter)'}
POLL = 10
latest = {'error': 'Starting', 'ticks': {}}


def short(tag):
    s = TAGS.get(tag, {}); t = f"{s.get('code', '')} M{s.get('tf', '')} {s.get('rule', '')}"
    return t + (f" · {s['stop_atr']:g} ATR stop · {s['target_R']:g}R target" if s.get('target_R') else f" · {s.get('stop_atr', 0):g} ATR stop · trail")


def snapshot():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=3)
    st = dict(c.execute('select k, v from state').fetchall())
    trades = c.execute('select tag, grp, code, side, lots, entry, exit, r_mult, pnl, open_ms, close_ms, reason, entry_spread, exit_spread from trades order by id').fetchall()
    pos = c.execute('select tag, grp, code, side, lots, entry, sl, tp, open_ms, entry_spread from pos').fetchall()
    skips = c.execute('select ts, tag, note from skips order by id desc limit 8').fetchall()
    c.close()
    return st, trades, pos, skips


def poll():
    global latest
    while True:
        ticks = {}
        try:
            with rpyc.classic.connect('127.0.0.1', 18812) as conn:
                conn._config['sync_request_timeout'] = 8; m = conn.modules.MetaTrader5
                for code, n in SYMBOLS.items():
                    t = m.symbol_info_tick(n)
                    if t:
                        ticks[code] = (float(t.bid), float(t.ask))
            latest = dict(error=None, ticks=ticks, at=time.time())
        except Exception:
            logging.exception('quotes unavailable'); latest = dict(error='Live quotes unavailable', ticks=latest.get('ticks', {}), at=latest.get('at'))
        time.sleep(POLL)


def table(headers, rows, empty):
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    labels = [html.escape(h, quote=True) for h in headers]
    body = ''.join('<tr>' + ''.join(f'<td data-label="{l}">{html.escape(str(v))}</td>' for l, v in zip(labels, r)) + '</tr>' for r in rows)
    return '<table><thead><tr>' + ''.join(f'<th scope="col">{l}</th>' for l in labels) + f'</tr></thead><tbody>{body}</tbody></table>'


utc = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%b %d %H:%M')


def page():
    try:
        st, trades, pos, skips = snapshot()
    except Exception:
        return '<!doctype html><p>Paper trader has not started yet.</p>'
    q = latest.get('ticks', {})
    started = dt.datetime.fromtimestamp(float(st.get('started', time.time())), dt.timezone.utc).strftime('%b %d %H:%M UTC')
    health = latest['error'] or 'Live Deriv quotes'
    cards = ''
    for g in ('jev', 'wr70'):
        eq = float(st.get(f'equity:{g}', 100)); T = [t for t in trades if t[1] == g]
        opnl = 0.0
        for p in pos:
            if p[1] == g and p[2] in q:
                bid, ask = q[p[2]]; opnl += p[3] * ((bid if p[3] > 0 else ask) - p[5]) * p[4]
        wins = sum(1 for t in T if t[8] > 0)
        spr = sum(t[12] for t in T) / len(T) if T else 0
        items = [('Equity', f'${eq + opnl:,.2f}'), ('Return', f'{100 * (eq + opnl - 100) / 100:+.1f}%'), ('Closed trades', f'{len(T)}'),
                 ('Win rate', f'{100 * wins / len(T):.0f}%' if T else '—'), ('Avg R', f'{sum(t[7] for t in T) / len(T):+.3f}' if T else '—'),
                 ('Open P&L', f'${opnl:+,.2f}')]
        cards += (f'<section class="summary"><h2>{html.escape(GLABEL[g])}</h2><p class="muted">Paper account · started at $100 · '
                  f'avg spread paid at entry {spr:.2f}</p><dl>' + ''.join(f'<div><dt>{k}</dt><dd>{v}</dd></div>' for k, v in items) + '</dl></section>')
    per = []
    for tag in TAGS:
        T = [t for t in trades if t[0] == tag]
        if not T:
            per.append((short(tag), TAGS[tag]['group'], 0, '—', '—', '—', '—')); continue
        per.append((short(tag), TAGS[tag]['group'], len(T), f'{100 * sum(t[8] > 0 for t in T) / len(T):.0f}%', f'{sum(t[7] for t in T) / len(T):+.3f}',
                    f'${sum(t[8] for t in T):+,.2f}', f'{sum(t[12] for t in T) / len(T):.2f}'))
    per.sort(key=lambda r: -r[2])
    op = []
    for p in pos:
        if p[2] in q:
            bid, ask = q[p[2]]; cur = bid if p[3] > 0 else ask; pl = p[3] * (cur - p[5]) * p[4]
        else:
            cur, pl = None, None
        op.append((short(p[0]), 'Buy' if p[3] > 0 else 'Sell', f'{p[4]:g}', f'{p[5]:.2f}', f'{cur:.2f}' if cur else '—', f'{p[6]:.2f}',
                   f'{p[7]:.2f}' if p[7] else 'trail', f'${pl:+.2f}' if pl is not None else '—', utc(p[8])))
    rec = [(utc(t[10]), short(t[0]), 'Buy' if t[3] > 0 else 'Sell', f'{t[4]:g}', f'{t[5]:.2f}', f'{t[6]:.2f}', t[11], f'{t[12]:.2f} / {t[13]:.2f}',
            f'{t[7]:+.2f}', f'${t[8]:+.2f}') for t in reversed(trades[-100:])]
    sk = ''.join(f'<li>{html.escape(dt.datetime.fromtimestamp(s[0], dt.timezone.utc).strftime("%b %d %H:%M"))} · {html.escape(short(s[1]))}: {html.escape(s[2])}</li>' for s in skips)
    body = (f'<div class="summaries">{cards}</div>'
            f'<section><h2>By strategy</h2>' + table(['Strategy', 'Group', 'Trades', 'Win rate', 'Avg R', 'Net', 'Avg entry spread'], per, '') + '</section>'
            f'<section><h2>Open paper positions</h2>' + table(['Strategy', 'Side', 'Lots', 'Entry', 'Current', 'Stop', 'Target', 'P&L', 'Opened (UTC)'], op, 'No open positions.') + '</section>'
            f'<section><h2>Closed paper trades</h2><p class="muted">Latest 100 of {len(trades)}. Entries and exits are real Deriv MT5 bid/ask quotes; '
            f'"Spread" shows the spread at entry / at exit.</p>' + table(['Closed (UTC)', 'Strategy', 'Side', 'Lots', 'Entry', 'Exit', 'Exit by', 'Spread', 'R', 'P&L'], rec, 'No closed trades yet.') + '</section>'
            + (f'<section><h2>Recent skipped signals</h2><ul class="muted">{sk}</ul></section>' if sk else ''))
    sub = f'{html.escape(health)} · paper results since {started} · no orders are sent to Deriv'
    return shell('dsi', sub, body)


def shell(active, sub, body):
    tabs = ''.join(f'<a href="{href}"' + (' aria-current="page"' if key == active else '') + f'>{label}</a>'
                   for key, href, label in (('dsi', '/', 'DSI strategies'), ('trek', '/trek', 'Trek spike-flip test'),
                                             ('flip1000', '/flip1000', 'Flip 1000 run-follow'), ('trekvs', '/trek-vshock', 'Trek Up vol shock'),
                                             ('be', '/breakeven', 'IC break-even (live)')))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta http-equiv="refresh" content="{POLL}"><title>Deriv Paper Trading</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&amp;display=swap">
<style>
:root {{ color-scheme:light dark; --bg:#fff; --surface:#f2f2f2; --text:#000; --muted:#595959; background:var(--bg); color:var(--text); font:13px/1.55 "DM Sans",system-ui,sans-serif; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#000; --surface:#141414; --text:#fff; --muted:#aaa; }} }}
* {{ box-sizing:border-box; }} body {{ max-width:1120px; margin:auto; padding:clamp(18px,5vw,52px) clamp(14px,4vw,38px); }}
h1,h2,p,dl,dd {{ margin:0; }} h1 {{ font-size:24px; line-height:1.2; font-weight:650; letter-spacing:-.045em; }}
h2 {{ font-size:14px; font-weight:650; }} .muted,dt {{ color:var(--muted); font-size:12px; }} header p, section > p {{ margin-top:5px; }}
main {{ display:grid; gap:30px; margin-top:24px; }} .summaries {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.summary {{ background:var(--surface); padding:14px 16px; border-radius:8px; }} dl {{ display:flex; gap:12px 24px; flex-wrap:wrap; margin-top:14px; }}
dd {{ font-size:18px; font-weight:600; letter-spacing:-.035em; font-variant-numeric:tabular-nums; }} ul {{ margin:4px 0 0; padding-left:18px; }}
table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; font-variant-numeric:tabular-nums; }}
td,th {{ text-align:left; padding:7px 8px; overflow-wrap:anywhere; }} td:first-child,th:first-child {{ padding-left:0; }}
td:last-child,th:last-child {{ text-align:right; padding-right:0; }} th {{ color:var(--muted); font-size:11px; font-weight:600; }}
@media (max-width:760px) {{ table,tbody {{ display:block; }} thead {{ position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }}
  tr {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,100px),1fr)); gap:5px 12px; padding:10px 0; }}
  td,td:first-child,td:last-child {{ display:block; min-width:0; padding:0; text-align:left; }}
  td::before {{ content:attr(data-label); display:block; color:var(--muted); font-size:11px; }} td[data-label="Strategy"] {{ grid-column:1/-1; font-weight:600; }}
  .summaries {{ grid-template-columns:1fr; }} }}
nav {{ display:flex; gap:4px; margin-top:16px; border-bottom:1px solid var(--surface); flex-wrap:wrap; }}
nav a {{ color:var(--muted); text-decoration:none; padding:7px 12px; border-radius:6px 6px 0 0; font-weight:600; }}
nav a[aria-current="page"] {{ color:var(--text); background:var(--surface); }}
.lead {{ background:var(--surface); padding:14px 16px; border-radius:8px; max-width:78ch; }} .lead p + p {{ margin-top:8px; }}
</style></head><body><header><h1>Deriv Paper Trading</h1><p class="muted">{sub}</p><nav aria-label="Paper tests">{tabs}</nav></header>
<main>{body}</main></body></html>"""


TREK_DB = os.path.join(BASE, 'data', 'trek.db')
TREK_NAMES = {'TREK_U': 'Trek Up', 'TREK_D': 'Trek Down'}
TREK_START = dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)
TREK_END = dt.datetime(2027, 2, 19, tzinfo=dt.timezone.utc)
TREK_LEAD = ('On both Trek Up and Trek Down, a spike sometimes appears to flip the drift. Fading that apparent flip for 30 seconds '
             'made +0.4 to +0.6 bp, and it was positive in every week (t 3.4 and 2.0). It\'s the only result that is consistent on both '
             'Trek mirrors, but it\'s still a chance-level t. It needs a test on data after today, not more mining of this sample.')


def trek_stats(rows, start_day, end_day):
    """rows: (exit_s, net_bps, pnl). Daily t uses every calendar day in the window (zero on days without trades)."""
    if not rows:
        return None
    net = [r[1] for r in rows]
    last = min(end_day, dt.datetime.now(dt.timezone.utc).date())
    ndays = max((last - start_day).days + 1, 1)
    daily = [0.0] * ndays
    for r in rows:
        k = (dt.datetime.fromtimestamp(r[0], dt.timezone.utc).date() - start_day).days
        if 0 <= k < ndays:
            daily[k] += r[1]
    mean_d = sum(daily) / ndays
    sd = (sum((d - mean_d) ** 2 for d in daily) / (ndays - 1)) ** 0.5 if ndays > 2 else 0
    weeks = [sum(daily[i:i + 7]) for i in range(0, ndays, 7)]
    return dict(n=len(net), mean=sum(net) / len(net), t=(mean_d / (sd / ndays ** 0.5)) if sd > 0 else 0.0,
                hit=sum(1 for x in net if x > 0) / len(net), usd=sum(r[2] for r in rows), wpos=sum(1 for w in weeks if w > 0), weeks=len(weeks))


def trek_page():
    try:
        c = sqlite3.connect(f'file:{TREK_DB}?mode=ro', uri=True, timeout=3)
        st = dict(c.execute('select k, v from state').fetchall())
        tr = c.execute('select code, entry_s, exit_s, side, entry, exit, entry_spread, exit_spread, net_bps, pnl_minlot, warmup from trades order by id').fetchall()
        daily = c.execute('select day, code, spikes, flips, taken, busy, invalid from daily order by day desc, code').fetchall()
        c.close()
    except Exception:
        return shell('trek', 'Trek spike-flip paper test', '<p class="muted">The Trek paper test has not started yet.</p>')
    now = time.time()
    lag = [now - float(st[f'last_run:{k}']) for k in TREK_NAMES if f'last_run:{k}' in st]
    health = (f'Processing ticks · last batch {max(lag):.0f} s ago' if lag and max(lag) < 120 else 'Not processing: check deriv-trek-paper')
    sd, ed = TREK_START.date(), (TREK_END - dt.timedelta(days=1)).date()
    live = [t for t in tr if not t[10]]; warm = [t for t in tr if t[10]]
    def card(title, rows, note):
        s = trek_stats([(t[2], t[8], t[9]) for t in rows], sd, ed)
        items = ([('Trades', f"{s['n']}"), ('Mean net', f"{s['mean']:+.3f} bp"), ('Daily t', f"{s['t']:+.2f}"), ('Win rate', f"{100 * s['hit']:.0f}%"),
                  ('Weeks positive', f"{s['wpos']}/{s['weeks']}"), ('P&L at min lot', f"${s['usd']:+,.2f}")] if s else [('Trades', '0')])
        return (f'<section class="summary"><h2>{html.escape(title)}</h2><p class="muted">{html.escape(note)}</p><dl>'
                + ''.join(f'<div><dt>{k}</dt><dd>{v}</dd></div>' for k, v in items) + '</dl></section>')
    cards = card('Pooled', live, 'Counts toward the verdict')
    cards += ''.join(card(TREK_NAMES[k], [t for t in live if t[0] == k], f"sigma {float(st.get('sigma:' + k, 0)) * 1e4:.3f} bp per second") for k in TREK_NAMES)
    S = {k: trek_stats([(t[2], t[8], t[9]) for t in live if t[0] == k], sd, ed) for k in TREK_NAMES}
    P = trek_stats([(t[2], t[8], t[9]) for t in live], sd, ed)
    nowd = dt.datetime.now(dt.timezone.utc)
    if nowd < TREK_START:
        verdict = f'Not started. Counting begins {TREK_START:%b %d %Y} 00:00 UTC; trades before then are warm-up and are listed separately.'
    else:
        ok = bool(P) and P['mean'] > 0 and P['t'] >= 2 and all(S[k] and S[k]['mean'] > 0 for k in TREK_NAMES)
        state = 'Final' if nowd >= TREK_END else 'In progress, provisional'
        verdict = f"{state}: {'meets' if ok else 'does not meet'} the pass rule so far."
    w = trek_stats([(t[2], t[8], t[9]) for t in warm], warm and dt.datetime.fromtimestamp(warm[0][2], dt.timezone.utc).date() or sd, sd)
    warm_line = (f"Warm-up before the start (not counted): {w['n']} trades, mean net {w['mean']:+.3f} bp." if w else 'No warm-up trades.')
    rec = [(dt.datetime.fromtimestamp(t[1], dt.timezone.utc).strftime('%b %d %H:%M:%S'), TREK_NAMES[t[0]], 'Buy' if t[3] > 0 else 'Sell',
            f'{t[4]:.2f}', f'{t[5]:.2f}', f'{t[6]:.2f} / {t[7]:.2f}', f'{t[8]:+.3f}', f'${t[9]:+.3f}', 'warm-up' if t[10] else '')
           for t in reversed(tr[-60:])]
    dly = [(d[0], TREK_NAMES.get(d[1], d[1]), d[2], d[3], d[4], d[5], d[6]) for d in daily[:28]]
    body = (f'<section class="lead"><h2>The one lead worth a follow-up</h2><p>{html.escape(TREK_LEAD)}</p>'
            f'<p class="muted">Correction to the numbers above: the rule fired only 105 times on Trek Up and 82 on Trek Down in 119 days '
            f'(under one trade per day per symbol). "Positive in every week" counted only weeks with at least 10 trades, which was 2 weeks '
            f'(Trek Up) and 1 week (Trek Down). The test therefore runs 12 weeks, enough for about 130 trades.</p>'
            f'<p class="muted">Rule, fixed before the test: a spike is a 1-second move larger than 8 robust sigma. 120 s after a spike, '
            f'if the jump-free drift since the spike has the opposite sign to the 300 s before it, trade against the new drift. '
            f'Entry 1 s later at the ask or bid, exit 30 s after entry at the bid or ask, one position per symbol. Fills are simulated on '
            f'recorded Deriv ticks; no orders are sent.</p>'
            f'<p class="muted">Pass rule: from {TREK_START:%b %d} to {TREK_END:%b %d %Y} (UTC), pooled mean net &gt; 0 with daily t &ge; 2, '
            f'and both Trek Up and Trek Down positive. Otherwise the lead is dropped.</p>'
            f'<p><strong>{html.escape(verdict)}</strong> <span class="muted">{html.escape(warm_line)}</span></p></section>'
            f'<div class="summaries">{cards}</div>'
            f'<section><h2>Daily activity</h2>' + table(['Day (UTC)', 'Symbol', 'Spikes', 'Flips', 'Trades', 'Skipped: open trade', 'Skipped: stale quote'], dly, 'No activity yet.') + '</section>'
            f'<section><h2>Paper trades</h2><p class="muted">Latest 60 of {len(tr)}. Net includes the quoted spread and swap; '
            f'P&L is at the minimum lot.</p>' + table(['Entry (UTC)', 'Symbol', 'Side', 'Entry', 'Exit', 'Spread', 'Net bp', 'P&L', ''], rec, 'No trades yet.') + '</section>')
    return shell('trek', health + ' · no orders are sent to Deriv', body)


LEADS_DB = os.path.join(BASE, 'data', 'leads.db')
LEAD_TABS = {
    'flip1000': dict(
        title='Flip 1000 run-follow',
        rules=[('flip1000_run', 'Primary: hold 30 min')],
        lead=('Crash Boom Flip 1000: after four consecutive same-direction one-second moves, trade in that direction and hold '
              '30 minutes. In the t \u2265 2 walk-forward it was picked in 5 of 13 test weeks and made +0.65 bp per trade out of '
              'sample over 1,479 trades, positive in 4 of 5 weeks. It fits the strong drift persistence measured on this index '
              '(variance ratio 4.6 at 60 minutes with spikes removed). But the per-symbol out-of-sample t was only 0.99, and the '
              'pooled walk-forward across all symbols lost money, so this is consistent with chance.'),
        rule=('Event: the 4th consecutive 1-second mid move in the same direction (Deriv quotes on a 1-second grid). Trade in that '
              'direction 1 s later at the ask or bid, exit 1,800 s later at the bid or ask, one position at a time, swap charged.')),
    'trekvs': dict(
        title='Trek Up volatility shock',
        rules=[('trekup_vs600', 'Primary: hold 10 min'), ('trekup_vs1800', 'Secondary: hold 30 min (reported only)')],
        lead=('Trek Up: when one-minute volatility jumps to more than three times its level over the previous 30 minutes, follow '
              'the last minute\'s move. In the t \u2265 2 walk-forward it was picked in 4 of 13 test weeks: +0.86 bp per trade '
              'over 506 trades with a 10-minute hold, and +2.4 bp over 428 trades with a 30-minute hold, positive in all 4 weeks. '
              'Four positive weeks out of many symbols and rules is the kind of streak chance produces, so treat it as a lead only.'),
        rule=('Event: 60-second realised variance of jump-free 1-second returns (moves above 8 robust sigma removed, sigma from the '
              'last 24 h) first exceeds 3\u00d7 its level over the preceding 1,800 s. Trade in the direction of the last 60 s '
              'jump-free move 1 s later at the ask or bid; exit after the hold at the bid or ask; one position per rule.')),
}


def lead_page(tab):
    L = LEAD_TABS[tab]; keys = [k for k, _ in L['rules']]
    try:
        c = sqlite3.connect(f'file:{LEADS_DB}?mode=ro', uri=True, timeout=3)
        st = dict(c.execute('select k, v from state').fetchall())
        q = ','.join('?' * len(keys))
        tr = c.execute(f'select lead, entry_s, exit_s, side, entry, exit, entry_spread, exit_spread, net_bps, pnl_minlot, warmup from trades where lead in ({q}) order by id', keys).fetchall()
        daily = c.execute(f'select day, lead, events, taken, busy, invalid from daily where lead in ({q}) order by day desc, lead', keys).fetchall()
        c.close()
    except Exception:
        return shell(tab, L['title'] + ' paper test', '<p class="muted">This paper test has not started yet.</p>')
    now = time.time()
    lag = [now - float(st[f'last_run:{k}']) for k in keys if f'last_run:{k}' in st]
    health = (f'Processing ticks · last batch {max(lag):.0f} s ago' if lag and max(lag) < 120 else 'Not processing: check deriv-leads-paper')
    sd, ed = TREK_START.date(), (TREK_END - dt.timedelta(days=1)).date()
    label = dict(L['rules']); cards = ''; stats = {}
    for k in keys:
        rows = [(t[2], t[8], t[9]) for t in tr if t[0] == k and not t[10]]
        s = stats[k] = trek_stats(rows, sd, ed)
        items = ([('Trades', f"{s['n']}"), ('Mean net', f"{s['mean']:+.3f} bp"), ('Daily t', f"{s['t']:+.2f}"), ('Win rate', f"{100 * s['hit']:.0f}%"),
                  ('Weeks positive', f"{s['wpos']}/{s['weeks']}"), ('P&L at min lot', f"${s['usd']:+,.2f}")] if s else [('Trades', '0')])
        cards += (f'<section class="summary"><h2>{html.escape(label[k])}</h2><p class="muted">Counted from {TREK_START:%b %d}</p><dl>'
                  + ''.join(f'<div><dt>{a}</dt><dd>{b}</dd></div>' for a, b in items) + '</dl></section>')
    nowd = dt.datetime.now(dt.timezone.utc); P = stats[keys[0]]
    if nowd < TREK_START:
        verdict = f'Not started. Counting begins {TREK_START:%b %d %Y} 00:00 UTC; trades before then are warm-up.'
    else:
        ok = bool(P) and P['mean'] > 0 and P['t'] >= 2
        verdict = f"{'Final' if nowd >= TREK_END else 'In progress, provisional'}: the primary rule {'meets' if ok else 'does not meet'} the pass rule so far."
    warm = [t for t in tr if t[10]]
    warm_line = (f'Warm-up before the start (not counted): {len(warm)} trades, mean net {sum(t[8] for t in warm) / len(warm):+.3f} bp.' if warm else 'No warm-up trades yet.')
    rec = [(dt.datetime.fromtimestamp(t[1], dt.timezone.utc).strftime('%b %d %H:%M:%S'), label[t[0]].split(':')[0], 'Buy' if t[3] > 0 else 'Sell',
            f'{t[4]:.2f}', f'{t[5]:.2f}', f'{t[6]:.2f} / {t[7]:.2f}', f'{t[8]:+.3f}', f'${t[9]:+.3f}', 'warm-up' if t[10] else '')
           for t in reversed(tr[-60:])]
    dly = [(d[0], label[d[1]].split(':')[0], d[2], d[3], d[4], d[5]) for d in daily[:28]]
    body = (f'<section class="lead"><h2>{html.escape(L["title"])}</h2><p>{html.escape(L["lead"])}</p>'
            f'<p class="muted">Rule, fixed before the test: {html.escape(L["rule"])} Fills are simulated on recorded Deriv ticks; '
            f'no orders are sent.</p><p class="muted">Pass rule: from {TREK_START:%b %d} to {TREK_END:%b %d %Y} (UTC), the primary rule\'s '
            f'mean net &gt; 0 with daily t &ge; 2. Otherwise the lead is dropped.</p>'
            f'<p><strong>{html.escape(verdict)}</strong> <span class="muted">{html.escape(warm_line)}</span></p></section>'
            f'<div class="summaries">{cards}</div>'
            f'<section><h2>Daily activity</h2>' + table(['Day (UTC)', 'Rule', 'Events', 'Trades', 'Skipped: open trade', 'Skipped: stale quote'], dly, 'No activity yet.') + '</section>'
            f'<section><h2>Paper trades</h2><p class="muted">Latest 60 of {len(tr)}. Net includes the quoted spread and swap; P&L is at the '
            f'minimum lot.</p>' + table(['Entry (UTC)', 'Rule', 'Side', 'Entry', 'Exit', 'Spread', 'Net bp', 'P&L', ''], rec, 'No trades yet.') + '</section>')
    return shell(tab, health + ' · no orders are sent to Deriv', body)


# ---------------- IC Markets live break-even monitor (read-only) ----------------
IC_DIR = os.path.normpath(os.path.join(BASE, '..', 'icmarkets-m8'))
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


def ic_poll():
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


def be_page():
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
    return shell('be', health + ' · read-only view of the live IC runner', body)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {'/': page, '/trek': trek_page, '/flip1000': lambda: lead_page('flip1000'), '/trek-vshock': lambda: lead_page('trekvs'), '/breakeven': be_page}
        if self.path not in routes:
            self.send_error(404); return
        b = routes[self.path]().encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=ic_poll, daemon=True).start()
    with ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PAPER_UI_PORT', '8092'))), H) as s:
        s.serve_forever()
