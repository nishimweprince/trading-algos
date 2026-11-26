"""Convert IC Markets daily tick exports (/opt/icmarkets-research/history/<sym>/ticks/*.npz) into the wf.py format
under $WF_DIR/ticks, plus shuffled controls (real step sizes and spreads in random order: a martingale with the same
distribution, so any pattern found there is a false positive)."""
import glob, json, os, sys
import numpy as np
SRC = '/opt/icmarkets-research/history'; D = os.environ.get('WF_DIR', '/opt/dsi-analogs/wf_ic'); os.makedirs(f'{D}/ticks', exist_ok=True)
CTRL = {'CTRL_STOCH': 'Stochastic_Core', 'CTRL_BOOM': 'Boom1000'}
for sym in sorted(os.listdir(SRC)):
    fs = sorted(glob.glob(f'{SRC}/{sym}/ticks/*.npz'))
    if not fs:
        continue
    rows = [np.load(f)['rows'] for f in fs]; rows = [r for r in rows if len(r)]
    ms = np.concatenate([r['time_msc'] for r in rows]).astype(np.int64)
    bid = np.concatenate([r['bid'] for r in rows]).astype(float); ask = np.concatenate([r['ask'] for r in rows]).astype(float)
    sj = json.load(open(f'{SRC}/{sym}/symbol.json'))
    spec = dict(name=sym, point=sj['point'], digits=sj['digits'], min_stop=sj['trade_stops_level'] * sj['point'], swap_mode=sj['swap_mode'],
                swap_long=sj['swap_long'], swap_short=sj['swap_short'], vmin=sj['volume_min'], tick_value=sj['trade_tick_value'],
                tick_size=sj['trade_tick_size'], contract=sj['trade_contract_size'])
    np.savez(f'{D}/ticks/{sym}.npz', ms=ms, bid=bid, ask=ask); json.dump(spec, open(f'{D}/ticks/{sym}.json', 'w'))
    print(sym, len(fs), 'days', len(ms), 'ticks', flush=True)
    for code, base in CTRL.items():
        if base == sym:
            ok = (bid > 0) & (ask > 0); m, b, a = ms[ok], bid[ok], ask[ok]
            mid = (a + b) / 2; sp = a - b; d = np.diff(np.log(mid)); rng = np.random.default_rng(11); rng.shuffle(d)
            sm = mid[0] * np.exp(np.r_[0, np.cumsum(d)])
            np.savez(f'{D}/ticks/{code}.npz', ms=m, bid=sm - sp / 2, ask=sm + sp / 2); json.dump(dict(spec, name=code), open(f'{D}/ticks/{code}.json', 'w'))
            print(code, 'control from', sym, flush=True)
