"""Item-4 diagnostic: baseline R by entry spread relative to the prior-hour median, in quintiles, per half."""
import sys, numpy as np
sys.path.insert(0, '/opt/dsi-analogs/jev'); import bt_jev as b
data = {c: b.load(c) for c in sorted({s['code'] for s in b.STRATS})}
rel, r, ems = [], [], []
for s in b.STRATS:
    X = data[s['code']]; g = b.signals(X, s)
    rr, _ = b.sim(X['ms'], X['bid'], X['ask'], g['ei'], g['side'], g['R'], np.ones(len(g['ei']), np.bool_), 1.5, 480, b.SWAP_MIN)
    m = np.isfinite(rr) & np.isfinite(g['rel']); rel += list(g['rel'][m]); r += list(rr[m]); ems += list(g['entry_ms'][m])
rel, r, ems = map(np.array, (rel, r, ems)); mid = np.median(ems)
qs = np.quantile(rel, [0, .2, .4, .6, .8, 1])
print('rel-spread quintile     range          n   mean R   ± se    1st half  2nd half')
for i in range(5):
    m = (rel >= qs[i]) & (rel <= qs[i + 1]) if i == 4 else (rel >= qs[i]) & (rel < qs[i + 1])
    x = r[m]
    print(f'  Q{i + 1}  {qs[i]:5.2f} - {qs[i + 1]:5.2f}   {m.sum():5}  {x.mean():+7.3f} {x.std(ddof=1) / np.sqrt(len(x)):6.3f}   '
          f'{r[m & (ems < mid)].mean():+7.3f}   {r[m & (ems >= mid)].mean():+7.3f}')
