"""Pull DAYS of Deriv MT5 ticks + contract spec per symbol into /opt/dsi-analogs/wf/ticks/. Read-only.
Skips symbols already saved. Usage: pull.py DAYS"""
import json, os, sys, time
import numpy as np, rpyc

OUT = '/opt/dsi-analogs/wf/ticks'; os.makedirs(OUT, exist_ok=True)
SYMS = {
    'CBF150': 'Crash Boom Flip 150 Index', 'CBF300': 'Crash Boom Flip 300 Index',
    'CBF500': 'Crash Boom Flip 500 Index', 'CBF1000': 'Crash Boom Flip 1000 Index',
    'VSW_L': 'VolSwitch Low Vol Index', 'VSW_M': 'VolSwitch Medium Vol Index', 'VSW_H': 'VolSwitch High Vol Index',
    'TREK_U': 'Trek Up Index', 'TREK_D': 'Trek Down Index',
    'RB100': 'Range Break 100 Index', 'RB200': 'Range Break 200 Index',
    'VB750': 'Volatility Boom 750 Index', 'VC750': 'Volatility Crash 750 Index',
    'DEX900U': 'DEX 900 UP Index', 'DEX900D': 'DEX 900 DOWN Index',
    'SKEW5U': 'Skew Step Index 5 Up', 'SKEW5D': 'Skew Step Index 5 Down',
    'EXP1': 'Exponential Growth Index 1', 'SNAP50': 'Snap 50 Index',
    'LADU': 'Ladder Volatility Up Index', 'LADD': 'Ladder Volatility Down Index',
    'V75': 'Volatility 75 Index',
}
days = int(sys.argv[1])
c = rpyc.classic.connect('127.0.0.1', 18812); c._config['sync_request_timeout'] = 600
c.execute('import MetaTrader5 as _m5'); m = c.modules.MetaTrader5; m.initialize()
end = int(time.time()) // 86400 * 86400
for code, n in SYMS.items():
    if os.path.exists(f'{OUT}/{code}.npz'):
        continue
    m.symbol_select(n, True); time.sleep(0.5)
    i = m.symbol_info(n)
    spec = dict(name=n, point=i.point, digits=i.digits, min_stop=i.trade_stops_level * i.point, swap_mode=i.swap_mode,
                swap_long=i.swap_long, swap_short=i.swap_short, vmin=i.volume_min, tick_value=i.trade_tick_value,
                tick_size=i.trade_tick_size, contract=i.trade_contract_size)
    parts, empty = [], 0
    for d in range(days, 0, -1):
        a = end - d * 86400
        c.execute(f'_tk = _m5.copy_ticks_range({n!r}, {a}, {a + 86400}, _m5.COPY_TICKS_ALL)\n'
                  f'_x = None if _tk is None or len(_tk) == 0 else (_tk["time_msc"].tobytes(), _tk["bid"].tobytes(), _tk["ask"].tobytes())')
        x = c.eval('_x')
        if x is None:
            empty += 1; continue
        parts.append(tuple(np.frombuffer(bytes(x[j]), np.int64 if j == 0 else np.float64) for j in range(3)))
    if not parts:
        print(code, 'no ticks', flush=True); continue
    ms, bid, ask = (np.concatenate([p[j] for p in parts]) for j in range(3))
    np.savez(f'{OUT}/{code}.npz', ms=ms, bid=bid, ask=ask)
    json.dump(spec, open(f'{OUT}/{code}.json', 'w'))
    print(code, len(ms), 'ticks', round((ms[-1] - ms[0]) / 864e5, 1), 'days span,', empty, 'empty days', flush=True)
print('DONE', flush=True)
