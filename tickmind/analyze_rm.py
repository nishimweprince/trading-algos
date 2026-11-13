"""Robustness checks for the risk-management grid (run after optimize_rm.py)."""
import json, sys
import numpy as np, pandas as pd
from backtest_signals import clean, load_bars
import optimize_rm as o

SIG, DATA = sys.argv[1], sys.argv[2]
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 90)
sig = clean(pd.read_csv(SIG)); bars = load_bars(DATA, sig.posted.min())
g = pd.read_csv("results/rm/grid.csv")
rng = np.random.default_rng(7)

def run(cfg, model, spread=0.30, delay=0.0, sides=None):
    tr = o.fills(sig, bars, model, spread, delay, sides).sort_values("posted").reset_index(drop=True)
    P = o.path_matrix(tr, bars, spread)
    r, pnl, why = o.simulate(P, tr, cfg, spread)
    return r, tr

def C(sl, tp, hold, be=None, trail=None, tp1=None, frac1=0):
    return dict(sl=sl, be=be, hold_h=hold, tp1=tp1, frac1=frac1, tp2=tp, trail=trail)

# 1. neighbourhood: fixed SL x single TP, market + touch, hold 8h and 24h
for model in ("market", "touch"):
    for hold in (8, 24):
        tab = {}
        for slv in (4, 6, 8, 10, 12, 15, 20):
            for tp in (0.5, 0.75, 1, 1.5, 2, 3):
                r, _ = run(C(("fixed", slv), tp, hold), model)
                tab[(f"SL ${slv}", f"TP {tp}R")] = r.mean()
        t = pd.Series(tab).unstack()[[f"TP {x}R" for x in (0.5, 0.75, 1, 1.5, 2, 3)]]
        print(f"\n[{model}, hold {hold}h] mean R per trade, full sample"); print(t.round(2).to_string())

# 2. candidates
cands = {
 "baseline: signal SL/TP, touch": ("touch", C(("signal", None), "signal", 120)),
 "baseline: signal SL/TP, market": ("market", C(("signal", None), "signal", 120)),
 "A: market, SL $12, TP 3R, 8h": ("market", C(("fixed", 12), 3, 8)),
 "B: market, SL $12, trail 1R after 2R, 8h": ("market", C(("fixed", 12), None, 8, trail=(2, 1))),
 "C: market, SL $12, 50%@1R + rest 3R, 8h": ("market", C(("fixed", 12), 3, 8, tp1=1, frac1=0.5)),
 "D: market, SL $10, TP 2R, 24h": ("market", C(("fixed", 10), 2, 24)),
 "E: touch, SL $12, TP 3R, 8h": ("touch", C(("fixed", 12), 3, 8)),
}
print("\n[candidates] mean R (t) under spread / delay stress")
rows = []
for name, (model, cfg) in cands.items():
    row = {"config": name}
    for sp, dl in [(0.30, 0), (0.30, 1), (0.30, 3), (0.50, 1), (0.80, 1)]:
        r, tr = run(cfg, model, sp, dl)
        row[f"sp{sp}/d{dl}m"] = f"{r.mean():+.2f} ({r.mean()/(r.std(ddof=1)/np.sqrt(len(r))):.1f}) n{len(r)}"
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))

# 3. bootstrap + monthly + side split for A..E at 1-min delay
print("\n[candidates @ spread 0.30, 1-min delay] bootstrap 90% CI of mean R, by month, by side")
for name, (model, cfg) in cands.items():
    r, tr = run(cfg, model, 0.30, 1)
    bs = np.array([rng.choice(r, len(r)).mean() for _ in range(4000)])
    mo = pd.Series(r, index=pd.to_datetime(tr.posted).dt.strftime("%m")).groupby(level=0).sum().round(1).to_dict()
    sd = pd.Series(r, index=tr.side).groupby(level=0).mean().round(2).to_dict()
    eq = np.cumsum(r); dd = np.max(np.maximum.accumulate(np.r_[0, eq])[1:] - eq)
    print(f"{name:44s} mean {r.mean():+.3f}  CI [{np.quantile(bs,.05):+.2f},{np.quantile(bs,.95):+.2f}]  P(mean<=0) {np.mean(bs<=0):.3f}  maxDD {dd:.1f}R  win {np.mean(r>0):.0%}  monthsR {mo}  side {sd}")

# 4. random-direction null for A and B: does the signal's side carry the edge?
print("\n[null] same times, same rules, random BUY/SELL (200 draws)")
for name in ("A: market, SL $12, TP 3R, 8h", "B: market, SL $12, trail 1R after 2R, 8h", "E: touch, SL $12, TP 3R, 8h"):
    model, cfg = cands[name]
    real, _ = run(cfg, model, 0.30, 1)
    ids = sig[sig.status == "closed"].id.values
    null = []
    for _ in range(200):
        sides = dict(zip(ids, rng.choice(["BUY", "SELL"], len(ids))))
        r, _ = run(cfg, model, 0.30, 1, sides)
        null.append(r.mean())
    null = np.array(null)
    print(f"{name:44s} real {real.mean():+.3f} | random mean {null.mean():+.3f} sd {null.std():.3f} | p(random >= real) {np.mean(null>=real.mean()):.3f}")
    rev = {i: ("SELL" if s == "BUY" else "BUY") for i, s in zip(sig.id, sig.side)}
    rr, _ = run(cfg, model, 0.30, 1, rev)
    print(f"{'':44s} reversed sides {rr.mean():+.3f}")

# 5. grid-wide selection bias: best train config in a random-direction world
print("\n[selection bias] max train mean R over the market single-TP/fixed-SL sub-grid, real vs 40 random-side worlds")
sub = [C(("fixed", s), tp, h) for s in (4, 6, 8, 10, 12, 15, 20) for tp in (0.5, 0.75, 1, 1.5, 2, 3) for h in (2, 8, 24, 120)]
def best_of(sides):
    tr = o.fills(sig, bars, "market", 0.30, 1, sides).sort_values("posted").reset_index(drop=True)
    P = o.path_matrix(tr, bars, 0.30)
    vals = [o.simulate(P, tr, c, 0.30)[0].mean() for c in sub]
    return max(vals), np.mean(vals)
real_best, real_avg = best_of(None)
nb = [best_of(dict(zip(sig.id, rng.choice(["BUY", "SELL"], len(sig))))) for _ in range(40)]
print(f"real: best {real_best:+.3f}, avg config {real_avg:+.3f} | random worlds: best mean {np.mean([x[0] for x in nb]):+.3f} (95th pct {np.quantile([x[0] for x in nb],.95):+.3f}), avg config {np.mean([x[1] for x in nb]):+.3f}")
