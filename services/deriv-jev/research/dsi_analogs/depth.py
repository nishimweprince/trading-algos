import time, rpyc
c = rpyc.classic.connect('127.0.0.1', 18812); c._config['sync_request_timeout'] = 300
c.execute('import MetaTrader5 as _m5'); m = c.modules.MetaTrader5; m.initialize()
names = ['Crash Boom Flip 500 Index', 'VolSwitch Medium Vol Index', 'Trek Up Index', 'Range Break 100 Index',
         'Volatility Boom 750 Index', 'DEX 900 UP Index', 'Skew Step Index 5 Up', 'Exponential Growth Index 1',
         'Snap 50 Index', 'Ladder Volatility Up Index']
now = int(time.time()) // 86400 * 86400
for n in names:
    m.symbol_select(n, True); time.sleep(0.5)
    c.execute(f'_r = _m5.copy_rates_from_pos({n!r}, _m5.TIMEFRAME_H1, 0, 50000)')
    first = c.eval('None if _r is None else int(_r[0][0])')
    out = []
    for d in (60, 90, 120, 180, 365):
        a = now - d * 86400
        c.execute(f'_t = _m5.copy_ticks_range({n!r}, {a}, {a + 3600}, _m5.COPY_TICKS_ALL)')
        out.append(f"{d}d:{c.eval('0 if _t is None else len(_t)')}")
    print(f"{n:30} H1 since {time.strftime('%Y-%m-%d', time.gmtime(first)) if first else None}  ticks/h at " + ' '.join(out), flush=True)
