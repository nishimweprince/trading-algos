import numpy as np, glob, os
print("code      jumps   VR15   VR60  VR240   ac60  ac240  |drift60|/sd")
for f in sorted(glob.glob("/opt/dsi-analogs/m1/*.npz")):
    d = np.load(f); c = d["c"].astype(float); r = np.diff(np.log(c))
    mad = np.median(np.abs(r - np.median(r))) * 1.4826 + 1e-15
    J = np.abs(r) > 8 * mad; rc = np.where(J, 0.0, r)      # jump bars removed
    L = np.r_[0, np.cumsum(rc)]
    def vr(k): rk = L[k:] - L[:-k]; return np.var(rk) / (k * np.var(rc))
    def ac(k): x = np.diff(L[::k]); return np.corrcoef(x[:-1], x[1:])[0, 1]
    x = np.diff(L[::60]); snr = np.mean(np.abs(x)) / (np.std(rc) * np.sqrt(60))
    print(f"{os.path.basename(f)[:-4]:8} {J.mean():6.4f} {vr(15):6.2f} {vr(60):6.2f} {vr(240):6.2f} {ac(60):6.2f} {ac(240):6.2f} {snr:8.2f}")
