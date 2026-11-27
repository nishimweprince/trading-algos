"""Pull DAYS of Deriv MT5 DSI10/20/30 ticks + contract spec into /opt/dsi-analogs/jev/ticks/. Read-only."""
import json, os, sys, time
import numpy as np, rpyc
OUT = '/opt/dsi-analogs/jev/ticks'; os.makedirs(OUT, exist_ok=True)
SYMS = {'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30'}
days = int(sys.argv[1])
c = rpyc.classic.connect('127.0.0.1', 18812); c._config['sync_request_timeout'] = 600
c.execute('import MetaTrader5 as _m5'); m = c.modules.MetaTrader5
end = int(time.time()) // 86400 * 86400 + 86400          # include today so far
for code, n in SYMS.items():
    m.symbol_select(n, True); time.sleep(0.5); i = m.symbol_info(n)
    spec = dict(name=n, point=i.point, digits=i.digits, min_stop=i.trade_stops_level * i.point, swap_mode=i.swap_mode,
                swap_long=i.swap_long, swap_short=i.swap_short, vmin=i.volume_min, tick_value=i.trade_tick_value, tick_size=i.trade_tick_size)
    parts, empty = [], 0
    for d in range(days, 0, -1):
        a = end - d * 86400
        c.execute(f'_tk = _m5.copy_ticks_range({n!r}, {a}, {a + 86400}, _m5.COPY_TICKS_ALL)\n'
                  f'_x = None if _tk is None or len(_tk) == 0 else (_tk["time_msc"].tobytes(), _tk["bid"].tobytes(), _tk["ask"].tobytes())')
        x = c.eval('_x')
        if x is None:
            empty += 1; continue
        parts.append(tuple(np.frombuffer(bytes(x[j]), np.int64 if j == 0 else np.float64) for j in range(3)))
    ms, bid, ask = (np.concatenate([p[j] for p in parts]) for j in range(3))
    np.savez(f'{OUT}/{code}.npz', ms=ms, bid=bid, ask=ask); json.dump(spec, open(f'{OUT}/{code}.json', 'w'))
    print(code, len(ms), 'ticks', round((ms[-1] - ms[0]) / 864e5, 1), 'days,', empty, 'empty days', spec, flush=True)
print('DONE', flush=True)
