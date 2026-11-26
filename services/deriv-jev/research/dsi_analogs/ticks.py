"""Pull N days of Deriv MT5 ticks (bid/ask/time_msc) per symbol into /opt/dsi-analogs/ticks/<code>.npz. Read-only."""
import sys, time, os
import numpy as np, rpyc
OUT = '/opt/dsi-analogs/ticks'; os.makedirs(OUT, exist_ok=True)
NAMES = {'CBF150': 'Crash Boom Flip 150 Index', 'CBF300': 'Crash Boom Flip 300 Index', 'CBF500': 'Crash Boom Flip 500 Index',
         'CBF1000': 'Crash Boom Flip 1000 Index', 'RB100': 'Range Break 100 Index', 'TREK_U': 'Trek Up Index',
         'TREK_D': 'Trek Down Index', 'DSI10': 'Drift Switch Index 10'}
days = int(sys.argv[1]); codes = sys.argv[2].split(',')
c = rpyc.classic.connect('127.0.0.1', 18812); c._config['sync_request_timeout'] = 600
c.execute('import MetaTrader5 as _m5'); m = c.modules.MetaTrader5; m.initialize()
end = int(time.time()) // 86400 * 86400
for code in codes:
    n = NAMES[code]; m.symbol_select(n, True); parts = []
    for d in range(days, 0, -1):
        a, b = end - d * 86400, end - (d - 1) * 86400
        c.execute(f'_tk = _m5.copy_ticks_range({n!r}, {a}, {b}, _m5.COPY_TICKS_ALL)\n'
                  f'_x = None if _tk is None else (_tk["time_msc"].tobytes(), _tk["bid"].tobytes(), _tk["ask"].tobytes())')
        x = c.eval('_x')
        if x is None:
            print(code, d, 'none', m.last_error(), flush=True); continue
        ms, bid, ask = (np.frombuffer(bytes(x[0]), np.int64), np.frombuffer(bytes(x[1]), np.float64), np.frombuffer(bytes(x[2]), np.float64))
        parts.append((ms, bid, ask))
    ms, bid, ask = (np.concatenate([p[i] for p in parts]) for i in range(3))
    np.savez_compressed(f'{OUT}/{code}.npz', ms=ms, bid=bid, ask=ask)
    print(code, len(ms), 'ticks', round((ms[-1] - ms[0]) / 864e5, 1), 'days', flush=True)
