"""Is retest-follow real?  (1) same pipeline on a simulated martingale with the real tick timestamps,
(2) split by side, (3) entry delay sweep, (4) unconditional drift baseline."""
import sys, json, numpy as np
sys.path.insert(0, '/opt/dsi-analogs/wf')
import wf
D = '/opt/dsi-analogs/wf'

def prep(code, sim=None):
    z = np.load(f'{D}/ticks/{code}.npz'); ms, bid, ask = z['ms'], z['bid'], z['ask']
    ok = (bid > 0) & (ask > 0) & (ask >= bid); ms, bid, ask = ms[ok], bid[ok], ask[ok]
    if sim == 'gauss':
        rng = np.random.default_rng(7); mid0 = (bid + ask) / 2
        steps = rng.standard_normal(len(ms)) * np.std(np.diff(np.log(mid0)))
        bid = ask = mid0[0] * np.exp(np.cumsum(steps))
    if sim == 'shuffle':   # real step sizes, random order: same distribution, no memory
        rng = np.random.default_rng(7); mid0 = (bid + ask) / 2; d = np.diff(np.log(mid0)); rng.shuffle(d)
        bid = ask = mid0[0] * np.exp(np.r_[0, np.cumsum(d)])
    s0 = int(ms[0] // 1000 // 86400 * 86400 + 86400); S = int(ms[-1] // 1000) - s0 + 1
    idx = wf.grid(ms, s0, S); valid = idx >= 0
    valid &= (s0 + np.arange(S)) * 1000 + 999 - ms[np.clip(idx, 0, None)] < 5000
    b = bid[np.clip(idx, 0, None)]; a = ask[np.clip(idx, 0, None)]; mid = (a + b) / 2
    return mid, b, a, valid

def retest_events(mid, N):
    pmax = wf.prior_ext(mid, N, 1); pmin = wf.prior_ext(mid, N, -1)
    bt, bs = wf.breaks(mid, pmax, pmin, 60); lvl = np.where(bs > 0, pmax[bt], pmin[bt])
    t, s = wf.retests(mid, bt, bs, lvl, 600); o = np.argsort(t, kind='stable'); return t[o], s[o]

def g(t, s, h, mid, b, a, valid, lat=1):
    te, net, gross = wf.trade(t + lat - 1, s, h, b, a, mid, valid, 0.0); m = te >= 0
    return m.sum(), gross[m].mean(), gross[m].std() / np.sqrt(max(m.sum(), 1))

for code in sys.argv[1:]:
    for sim in (None, 'shuffle', 'gauss'):
        mid, b, a, valid = prep(code, sim)
        t, s = retest_events(mid, 900)
        n, mu, se = g(t, s, 120, mid, b, a, valid)
        line = f"{code:8} {str(sim or 'real'):7} retest900 follow 120s: n {n} gross {mu:+.3f}bp (t {mu/se:+.1f})"
        if sim is None:
            for side in (1, -1):
                m = s == side; n2, mu2, se2 = g(t[m], s[m], 120, mid, b, a, valid)
                line += f" | side{side:+d} n{n2} {mu2:+.3f} (t{mu2/se2:+.1f})"
            line += ' | delay:'
            for lat in (1, 3, 10, 30, 120):
                n3, mu3, se3 = g(t, s, 120, mid, b, a, valid, lat)
                line += f" {lat}s {mu3:+.3f}"
            samp = np.arange(1000, len(mid) - 200, 60)
            n4, mu4, se4 = g(samp, np.ones(len(samp), np.int64), 120, mid, b, a, valid)
            line += f" | random-long 120s {mu4:+.3f} (t{mu4/se4:+.1f})"
        print(line, flush=True)
