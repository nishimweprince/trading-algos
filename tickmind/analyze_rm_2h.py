"""Stress the short-hold (2h) finalists the same way as analyze_rm.py."""
import sys, numpy as np, pandas as pd
from backtest_signals import clean, load_bars
import optimize_rm as o
sig = clean(pd.read_csv(sys.argv[1])); bars = load_bars(sys.argv[2], sig.posted.min()); rng = np.random.default_rng(11)
def C(sl, tp, hold, be=None, trail=None, tp1=None, frac1=0): return dict(sl=sl, be=be, hold_h=hold, tp1=tp1, frac1=frac1, tp2=tp, trail=trail)
def run(cfg, spread=0.30, delay=1.0, sides=None):
    tr = o.fills(sig, bars, "market", spread, delay, sides).sort_values("posted").reset_index(drop=True)
    P = o.path_matrix(tr, bars, spread); return o.simulate(P, tr, cfg, spread)[0], tr
print("[market, 1-min delay] mean R by hold (SL $12, TP 1R) and SL x TP grid at 2h / 4h")
for h in (1, 2, 3, 4, 6, 8, 24): print(f"  hold {h}h: {run(C(('fixed',12),1,h))[0].mean():+.3f}")
for hold in (2, 4):
    tab = {(f"SL ${s}", f"TP {tp}R"): run(C(("fixed", s), tp, hold))[0].mean() for s in (6, 8, 10, 12, 15, 20) for tp in (0.5, 0.75, 1, 1.5, 2, 3)}
    print(f"\n hold {hold}h"); print(pd.Series(tab).unstack()[[f"TP {x}R" for x in (0.5,0.75,1,1.5,2,3)]].round(2).to_string())
cands = {"F: SL $12, TP 1R, exit at 2h": C(("fixed",12),1,2),
         "G: SL $12, 50%@1R + trail 0.5R after 1R, 2h": C(("fixed",12),None,2,tp1=1,frac1=0.5,trail=(1,0.5)),
         "C: SL $12, 50%@1R + rest 3R, 8h": C(("fixed",12),3,8,tp1=1,frac1=0.5),
         "H: SL $12, 50%@1R + rest 3R, 4h": C(("fixed",12),3,4,tp1=1,frac1=0.5)}
print("\n[stress] mean R (t)")
for n, c in cands.items():
    cells = []
    for sp, dl in [(0.3,0),(0.3,1),(0.3,3),(0.3,5),(0.5,1),(0.8,1)]:
        r,_ = run(c, sp, dl); cells.append(f"sp{sp}/d{dl}: {r.mean():+.2f} ({r.mean()/(r.std(ddof=1)/np.sqrt(len(r))):.1f})")
    print(f"{n:46s} " + " | ".join(cells))
print("\n[detail @ sp0.30, 1-min delay]")
ids = sig[sig.status=="closed"].id.values
for n, c in cands.items():
    r, tr = run(c)
    bs = np.array([rng.choice(r, len(r)).mean() for _ in range(4000)])
    nul = np.array([run(c, sides=dict(zip(ids, rng.choice(["BUY","SELL"], len(ids)))))[0].mean() for _ in range(150)])
    mo = pd.Series(r, index=pd.to_datetime(tr.posted).dt.strftime("%m")).groupby(level=0).sum().round(1).to_dict()
    half = len(r)//2; eq=np.cumsum(r); dd=np.max(np.maximum.accumulate(np.r_[0,eq])[1:]-eq)
    l = (r<0).astype(int); streak = max((len(x) for x in "".join(map(str,l)).split("0")), default=0)
    print(f"{n:46s} mean {r.mean():+.3f} CI90 [{np.quantile(bs,.05):+.2f},{np.quantile(bs,.95):+.2f}] | 1st half {r[:half].mean():+.2f} 2nd half {r[half:].mean():+.2f} | win {np.mean(r>0):.0%} maxDD {dd:.1f}R worst streak {streak} | random-side p {np.mean(nul>=r.mean()):.3f} | months {mo}")
