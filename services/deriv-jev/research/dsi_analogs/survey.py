"""Survey Deriv MT5 synthetic candidates: cost, structure, history depth. Read-only.
Writes per-symbol M1 arrays to /opt/dsi-analogs/m1/<code>.npz and spread samples for the backtest."""
import json, os, time, sys
import numpy as np, rpyc

OUT = '/opt/dsi-analogs'; os.makedirs(OUT + '/m1', exist_ok=True)
SYMS = {
    'DSI10': 'Drift Switch Index 10', 'DSI20': 'Drift Switch Index 20', 'DSI30': 'Drift Switch Index 30',
    'CBF150': 'Crash Boom Flip 150 Index', 'CBF300': 'Crash Boom Flip 300 Index',
    'CBF500': 'Crash Boom Flip 500 Index', 'CBF1000': 'Crash Boom Flip 1000 Index',
    'VSW_L': 'VolSwitch Low Vol Index', 'VSW_M': 'VolSwitch Medium Vol Index', 'VSW_H': 'VolSwitch High Vol Index',
    'TREK_U': 'Trek Up Index', 'TREK_D': 'Trek Down Index',
    'RB100': 'Range Break 100 Index', 'RB200': 'Range Break 200 Index',
    'V75': 'Volatility 75 Index',  # random-walk control
}
c = rpyc.classic.connect('127.0.0.1', 18812); c._config['sync_request_timeout'] = 300
c.execute('import MetaTrader5 as _m5'); m = c.modules.MetaTrader5; m.initialize()
now = int(time.time()); rep = {}
for code, name in SYMS.items():
    m.symbol_select(name, True); time.sleep(0.3)
    i = m.symbol_info(name)
    if i is None:
        print(code, 'no info'); continue
    # M1 history, as deep as the server gives
    c.execute(f'_r = _m5.copy_rates_from_pos({name!r}, _m5.TIMEFRAME_M1, 0, 99999)')
    r = rpyc.classic.obtain(c.eval('_r'))
    t = (r['time'] // 60).astype(np.int64)
    np.savez_compressed(f'{OUT}/m1/{code}.npz', t=t, o=r['open'], h=r['high'], l=r['low'], c=r['close'], sp=r['spread'] * i.point)
    # last 24h of ticks for spread and tick rate
    c.execute(f'_tk = _m5.copy_ticks_range({name!r}, {now - 86400}, {now}, _m5.COPY_TICKS_ALL)')
    tk = rpyc.classic.obtain(c.eval('_tk'))
    bid, ask, ms = tk['bid'].astype(float), tk['ask'].astype(float), tk['time_msc'].astype(np.int64)
    ok = (bid > 0) & (ask > 0); bid, ask, ms = bid[ok], ask[ok], ms[ok]
    spr = ask - bid; mid = (ask + bid) / 2
    # trend-conditional spread: |15-min mid move| relative to rolling 15-min move std
    mm = ms // 60000; um, idx = np.unique(mm, return_index=True); mmid = mid[idx]
    d15 = np.full(len(um), np.nan); d15[15:] = mmid[15:] - mmid[:-15]
    sd = np.nanstd(d15); z = np.abs(d15) / sd
    zt = np.interp(mm, um, np.nan_to_num(z))
    # M1 structure
    lc = np.log(r['close'].astype(float)); ret = np.diff(lc)
    def vr(k):
        rk = lc[k:] - lc[:-k]; return float(np.var(rk) / (k * np.var(ret)))
    s60 = float(np.std(lc[60:] - lc[:-60]) * r['close'][-1])  # 60-min move in price units
    rb = np.abs(np.diff(r['close'].astype(float)))
    rep[code] = dict(name=name, point=i.point, digits=i.digits, spread_pts_now=i.spread, spread_now=i.spread * i.point,
        stops_level=i.trade_stops_level, min_stop=i.trade_stops_level * i.point, vmin=i.volume_min, vstep=i.volume_step,
        tick_value=i.trade_tick_value, tick_size=i.trade_tick_size, swap_long=i.swap_long, swap_short=i.swap_short, swap_mode=i.swap_mode,
        price=float(r['close'][-1]), m1_bars=len(t), m1_days=round((t[-1] - t[0]) / 1440, 1),
        ticks_24h=len(spr), ticks_per_min=round(len(spr) / 1440, 1),
        spread_mean=float(spr.mean()), spread_p50=float(np.median(spr)), spread_p95=float(np.percentile(spr, 95)),
        spread_quiet=float(spr[zt < 1].mean()) if (zt < 1).any() else None,
        spread_trend2=float(spr[zt > 2].mean()) if (zt > 2).any() else None,
        spread_bps=float(spr.mean() / mid.mean() * 1e4), move60=s60, spread_over_move60=float(spr.mean() / s60),
        vr5=vr(5), vr15=vr(15), vr60=vr(60), vr240=vr(240),
        jump_share=float((rb > 10 * np.median(rb) + 1e-12).mean()))
    print(code, json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in rep[code].items() if k != 'name'}), flush=True)
json.dump(rep, open(f'{OUT}/survey.json', 'w'), indent=1)
