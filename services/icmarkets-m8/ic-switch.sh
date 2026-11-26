#!/bin/bash
# IC Markets runner switch.  Usage:
#   ./ic-switch.sh status   account, Algo Trading, sizing preview, service state, recent activity
#   ./ic-switch.sh shadow   run in shadow mode (orders checked by IC, never sent)
#   ./ic-switch.sh live     pre-flight checks, typed confirmation, then real orders on the IC_LOGIN account
#   ./ic-switch.sh stop     stop the service (open positions keep their broker stop/target)
set -u
cd /opt/trading-algos/services/icmarkets-m8 || exit 1
PY=/opt/trading-algos/services/deriv-jev/.venv/bin/python
setmode() { sed -i "s/^IC_MODE=.*/IC_MODE=$1/" .env; }

preflight() {
  timeout 90 "$PY" - <<'EOF'
import os, sys, math, rpyc
env = dict(l.strip().split('=', 1) for l in open('.env') if '=' in l and not l.startswith('#'))
c = rpyc.classic.connect('127.0.0.1', int(env.get('IC_PORT', '18814'))); c._config['sync_request_timeout'] = 80
c.execute('import MetaTrader5 as _m5')
c.namespace['_t'] = env.get('IC_TERMINAL', r'C:\IC Markets Exec\terminal64.exe')
c.execute('_ok = _m5.initialize(path=_t, portable=True, timeout=60000)')
if not c.eval('_ok'):
    print('FAIL  terminal not reachable:', c.eval('str(_m5.last_error())'), '- is it waiting at the login box? (noVNC)'); sys.exit(2)
m = c.modules.MetaTrader5; a = m.account_info(); t = m.terminal_info()
ok = True
def chk(cond, good, bad):
    global ok
    print(('OK    ' if cond else 'FAIL  ') + (good if cond else bad)); ok &= bool(cond)
chk(a is not None and str(a.login) == env['IC_LOGIN'] and a.server == env['IC_SERVER'],
    f'account {a.login if a else "?"} on {a.server if a else "?"} ({a.company if a else ""}, {"real" if a and a.trade_mode == 2 else "demo"})',
    f'logged into {a.login if a else None}@{a.server if a else None}, expected {env["IC_LOGIN"]}@{env["IC_SERVER"]}')
chk(t.connected, 'terminal connected to the trade server', 'terminal NOT connected to the trade server')
chk(t.trade_allowed, 'Algo Trading is ON', 'Algo Trading is OFF: click the "Algo Trading" button in the IC Markets Exec terminal (noVNC)')
for sym in ('Boom1000', 'Jump50'):
    m.symbol_select(sym, True)
import time; time.sleep(2)
for sym in ('Boom1000', 'Jump50'):
    q = m.symbol_info_tick(sym)
    lag = (time.time() + round((q.time_msc / 1000 - time.time()) / 3600) * 3600 - q.time_msc / 1000) if q and q.time_msc else None
    chk(lag is not None and lag < 30, f'{sym} quotes are live ({lag:.1f}s old)' if lag is not None else '', f'{sym} quotes are stale or missing ({lag if lag is None else round(lag)}s)')
chk(a.trade_allowed and a.trade_expert, 'account allows automated trading', 'account does not allow automated trading')
eq = float(a.equity); risk = float(env.get('IC_RISK_PCT', '5')); cap = float(env.get('IC_MAX_RISK_PCT', '10'))
print(f'      balance ${a.balance:.2f}  equity ${eq:.2f}  free margin ${a.margin_free:.2f}  leverage 1:{a.leverage}')
print(f'      sizing: {risk:g}% of equity per trade; minimum 0.1 lot allowed up to {cap:g}% risk')
for sym, R in (('Boom1000', 75.7), ('Jump50', 28.6)):     # typical stop distances (5 ATR) from the backtest
    m.symbol_select(sym, True)
    import time; tk = None
    for _ in range(10):                                   # a just-selected symbol may need a moment for its first quote
        tk = m.symbol_info_tick(sym)
        if tk and tk.ask > 0: break
        time.sleep(0.5)
    i = m.symbol_info(sym); val = i.trade_tick_value / i.trade_tick_size
    lots = math.floor(eq * risk / 100 / (R * val) / i.volume_step + 1e-9) * i.volume_step
    if lots < i.volume_min:
        lots = i.volume_min if i.volume_min * R * val <= eq * cap / 100 else 0
    mg = m.order_calc_margin(m.ORDER_TYPE_BUY, sym, max(lots, i.volume_min), tk.ask) if tk and tk.ask > 0 else None
    mgs = f'${mg:.2f}' if mg is not None else 'n/a (no quote yet)'
    if lots:
        print(f'      {sym:9s} typical trade: {lots:.1f} lots, stop loss ~${lots * R * val:.2f} ({100 * lots * R * val / eq:.1f}%), margin {mgs}')
    else:
        print(f'      {sym:9s} would be SKIPPED: even 0.1 lot risks ~${i.volume_min * R * val:.2f} ({100 * i.volume_min * R * val / max(eq, 1e-9):.0f}% of equity); needs ~${i.volume_min * R * val * 100 / cap:.0f}+')
chk(eq >= 76, f'equity ${eq:.2f} covers both strategies', f'equity ${eq:.2f} is too low: about $76 is needed before Boom1000 can trade, ~$29 for Jump50 (top up)')
mine = [p for p in (m.positions_get() or []) if p.magic in (709001, 709002, 709003, 709004)]
others = [p for p in (m.positions_get() or []) if p.magic not in (709001, 709002, 709003, 709004)]
print(f'      open positions: {len(mine)} from this runner, {len(others)} other')
sys.exit(0 if ok else 1)
EOF
}

case "${1:-status}" in
  status)
    grep -E '^IC_(MODE|RISK_PCT|MAX_RISK_PCT|ENABLE)' .env
    echo "service: $(systemctl is-active ic-m8)"
    preflight
    "$PY" ic_m8.py status 2>/dev/null | tail -12
    tail -5 logs/ic_m8.log ;;
  shadow)
    setmode shadow; systemctl restart ic-m8; sleep 5
    echo "Shadow mode on (service $(systemctl is-active ic-m8))."; tail -3 logs/ic_m8.log ;;
  stop)
    systemctl stop ic-m8; echo "Stopped. Any open positions keep their stop/target at IC." ;;
  live)
    echo "== Pre-flight checks =="
    if ! preflight; then echo; echo "Not switching: fix the FAIL lines above first."; exit 1; fi
    echo
    echo "This sends REAL orders on IC Markets account $(grep ^IC_LOGIN .env | cut -d= -f2), risking $(grep ^IC_RISK_PCT .env | cut -d= -f2)% of equity per trade."
    echo "These strategies are unproven: picked from 108 backtests, with an 85% drawdown at 5% risk even in the backtest."
    read -r -p "Type LIVE to switch, anything else to cancel: " ans
    [ "$ans" = "LIVE" ] || { echo "Cancelled. Mode unchanged."; exit 0; }
    setmode live; systemctl restart ic-m8
    echo "Switched to LIVE. Following the log for 70 s (Ctrl-C to stop watching; the service keeps running)..."
    timeout 70 tail -n 3 -f logs/ic_m8.log ;;
  *) echo "usage: $0 status|shadow|live|stop"; exit 1 ;;
esac
