"""Print the MT5 runner's orders and exits: python mt5_report.py [n]"""
import os, sqlite3, sys, time
db = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'mt5.db'))
n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
print('ORDERS')
for r in db.execute('select id, ts, mode, tag, side, lots, price, sl, tp, round(strat_sl_d,2), round(sl_d,2), round(risk_usd,2), round(risk_pct,2), retcode, retmsg, note from orders order by id desc limit ?', (n,)):
    print(time.strftime('%m-%d %H:%M', time.gmtime(r[1])), r[0], *r[2:])
print('EXITS')
for r in db.execute('select ts, mode, tag, reason, price, round(pnl,2), order_id from exits order by id desc limit ?', (n,)):
    print(time.strftime('%m-%d %H:%M', time.gmtime(r[0])), *r[1:])
tot = db.execute('select count(*), round(sum(pnl),2), sum(pnl>0) from exits').fetchone()
print('closed', tot[0], 'pnl $', tot[1], 'wins', tot[2])
for m in ('shadow', 'live'):
    x = db.execute('select count(*), round(coalesce(sum(pnl),0),2), coalesce(sum(pnl>0),0) from exits where mode=?', (m,)).fetchone()
    print(m, 'closed', x[0], 'pnl $', x[1], 'wins', x[2])
