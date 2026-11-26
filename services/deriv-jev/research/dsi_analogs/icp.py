import numpy as np, glob, os
B = '/opt/icmarkets-research/history'
print("sym               days  VR15  VR60 VR240  ac60  spread_bps  sprd/move60  ticks/min")
for sym in sorted(os.listdir(B)):
    m1 = sorted(glob.glob(f'{B}/{sym}/m1/*.npz')); tk = sorted(glob.glob(f'{B}/{sym}/ticks/*.npz'))[-5:]
    if not m1: continue
    segs = [np.load(f)['rows'] for f in m1]; segs = [s for s in segs if len(s) > 100]
    # per-day log returns (no cross-gap returns), jump bars removed
    rc_all, vr = [], {15: [0, 0], 60: [0, 0], 240: [0, 0]}; ac = []
    allr = np.concatenate([np.diff(np.log(s['close'].astype(float))) for s in segs])
    mad = np.median(np.abs(allr)) * 1.4826 + 1e-15
    for s in segs:
        r = np.diff(np.log(s['close'].astype(float))); r = np.where(np.abs(r) > 8 * mad, 0, r); L = np.r_[0, np.cumsum(r)]
        rc_all.append(r)
        for k in vr:
            if len(L) > k: x = L[k:] - L[:-k]; vr[k][0] += (x ** 2).sum(); vr[k][1] += len(x) * k
        x = np.diff(L[::60]); ac += list(zip(x[:-1], x[1:]))
    v1 = np.mean(np.concatenate(rc_all) ** 2)
    a = np.array(ac); acc = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
    T = np.concatenate([np.load(f)['rows'] for f in tk]); T = T[(T['bid'] > 0) & (T['ask'] > 0)]
    spr = (T['ask'] - T['bid']).astype(float); mid = ((T['ask'] + T['bid']) / 2).astype(float)
    mv60 = np.std(np.concatenate([np.log(s['close'][60:].astype(float)) - np.log(s['close'][:-60].astype(float)) for s in segs])) * mid.mean()
    tpm = len(T) / ((T['time_msc'][-1] - T['time_msc'][0]) / 60000)
    print(f"{sym:17} {len(segs):4} {vr[15][0]/vr[15][1]/v1:5.2f} {vr[60][0]/vr[60][1]/v1:5.2f} {vr[240][0]/vr[240][1]/v1:5.2f} {acc:5.2f} {spr.mean()/mid.mean()*1e4:10.2f} {spr.mean()/mv60:11.3f} {tpm:9.1f}")
