"""Risk-management search on Tickmind XAUUSD signals.

Uses only the signal's DIRECTION and (optionally) its stated entry; stop, targets,
breakeven, trailing, partials and max hold are ours. Every trade is simulated on
the M1 path after the fill, vectorised across trades.

Conventions (same as backtest_signals.py):
  * HistData bid bars, EST+5h -> UTC; ask = bid + spread. Longs fill on ask and exit
    on bid; shorts fill on bid and exit on ask.
  * Within a bar, a stop hit beats a target hit (conservative). Stop and trail
    levels use excursions up to the PREVIOUS bar (no look-ahead inside the bar).
  * A bar that opens through the stop exits at the open (gap fill).
  * R = pnl / chosen stop distance, i.e. fixed-fractional risk per trade.

Selection protocol: configs are ranked on the TRAIN half (earlier signals) and
judged on the TEST half (later signals). Full-sample numbers are reported too,
but the test column is the one that matters.

Usage: python optimize_rm.py signals_raw.csv /path/to/XAUUSD [--spread 0.30] [--out results/rm]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os

import numpy as np
import pandas as pd

from backtest_signals import LIMIT_WAIT, MAX_BAR_GAP, MAX_POST_GAP, clean, load_bars

HOLD_MAX = pd.Timedelta(days=5)
INF = np.inf


# --------------------------------------------------------------------------- fills
def fills(sig: pd.DataFrame, bars: pd.DataFrame, model: str, spread: float,
          delay_min: float = 0.0, sides: dict | None = None) -> pd.DataFrame:
    """One row per filled trade: id, posted, side, i_fill, fill_px, sig_stop_d, sig_tp_d.

    delay_min: we act this many minutes after the post (alert + execution latency).
    sides: optional {id: "BUY"/"SELL"} override, used for random-direction null tests.
    """
    idx, o, h, l = bars.index, bars["o"].values, bars["h"].values, bars["l"].values
    rows = []
    for _, s in sig.iterrows():
        if s.status != "closed" or "excluded" in s.note:
            continue
        t_act = s.posted + pd.Timedelta(minutes=delay_min)
        i0 = idx.searchsorted(t_act.ceil("min"))
        if i0 >= len(bars) or idx[i0] - t_act > MAX_POST_GAP:
            continue
        px = o[i0]
        side = sides.get(s.id, s.side) if sides else s.side
        long = side == "BUY"
        if model == "market":
            sig_long = s.side == "BUY"   # the stale-post filter uses the signal's own levels
            if (sig_long and (px <= s.stop or px >= s.target)) or (not sig_long and (px >= s.stop or px <= s.target)):
                continue
            i_fill, fill = i0, px + (spread if long else 0.0)
        else:  # touch: pending order at stated entry, from whichever side price is on
            j1 = idx.searchsorted(t_act + LIMIT_WAIT, side="right")
            off = spread if long else 0.0
            if px + off > s.entry:
                hit = np.flatnonzero(l[i0:j1] + off <= s.entry)
            else:
                hit = np.flatnonzero(h[i0:j1] + off >= s.entry)
            if not len(hit):
                continue
            i_fill, fill = i0 + hit[0], s.entry
        rows.append(dict(id=s.id, posted=s.posted, side=side, i_fill=i_fill, fill_px=fill,
                         sig_stop_d=abs(s.entry - s.stop), sig_tp_d=abs(s.target - s.entry)))
    return pd.DataFrame(rows)


def path_matrix(tr: pd.DataFrame, bars: pd.DataFrame, spread: float):
    """Signed per-bar excursions from the fill, shape (n_trades, T).

    F: best favourable move inside the bar, A: worst adverse move, O: open, C: close,
    M: minutes since fill (inf where invalid / past a data hole).
    """
    idx = bars.index.values
    o, h, l, c = (bars[k].values for k in "ohlc")
    n = len(tr)
    T = int(HOLD_MAX / pd.Timedelta(minutes=1)) + 10
    F = np.full((n, T), np.nan, np.float32); A = F.copy(); O = F.copy(); C = F.copy()
    M = np.full((n, T), np.inf, np.float32)
    for k, r in enumerate(tr.itertuples()):
        i, t0 = r.i_fill, idx[r.i_fill]
        j = min(len(bars), i + T)
        tt = idx[i:j]
        gap = np.flatnonzero(np.diff(tt) > np.timedelta64(MAX_BAR_GAP))
        if len(gap):
            j = i + gap[0] + 1
            tt = idx[i:j]
        mins = ((tt - t0) / np.timedelta64(1, "m")).astype(np.float32)
        keep = mins <= HOLD_MAX / pd.Timedelta(minutes=1)
        m = int(keep.sum())
        if r.side == "BUY":   # long exits on bid
            F[k, :m] = h[i:i + m] - r.fill_px; A[k, :m] = l[i:i + m] - r.fill_px
            O[k, :m] = o[i:i + m] - r.fill_px; C[k, :m] = c[i:i + m] - r.fill_px
        else:                 # short exits on ask = bid + spread
            F[k, :m] = r.fill_px - (l[i:i + m] + spread); A[k, :m] = r.fill_px - (h[i:i + m] + spread)
            O[k, :m] = r.fill_px - (o[i:i + m] + spread); C[k, :m] = r.fill_px - (c[i:i + m] + spread)
        M[k, :m] = mins[:m]
    last = np.where(np.isfinite(M), np.arange(T), -1).max(1)
    T_used = int(last.max()) + 1
    return F[:, :T_used], A[:, :T_used], O[:, :T_used], C[:, :T_used], M[:, :T_used]


# --------------------------------------------------------------------------- simulator
def sim_leg(P, sl, tp, be, act, trail, hold_min):
    """Vectorised exit for one position leg. All distances are $ arrays of shape (n,).

    Returns pnl ($) per trade and exit reason codes (0 stop, 1 target, 2 time).
    """
    F, A, O, C, M = P
    n, T = F.shape
    valid = M <= hold_min
    Fv = np.where(valid, F, -np.inf)
    run = np.maximum.accumulate(Fv, axis=1)
    prev = np.concatenate([np.full((n, 1), -np.inf, np.float32), run[:, :-1]], axis=1)
    stop = np.broadcast_to(-sl[:, None], (n, T)).astype(np.float32)
    stop = np.where(prev >= be[:, None], np.maximum(stop, 0.0), stop)
    stop = np.where(prev >= act[:, None], np.maximum(stop, prev - trail[:, None]), stop)
    hit_sl = valid & (A <= stop)
    hit_tp = valid & (F >= tp[:, None])
    any_hit = hit_sl | hit_tp
    has = any_hit.any(1)
    first = np.where(has, any_hit.argmax(1), 0)
    rows = np.arange(n)
    last_valid = np.where(valid.any(1), valid.shape[1] - 1 - valid[:, ::-1].argmax(1), 0)
    is_sl = hit_sl[rows, first]
    st = stop[rows, first]
    sl_px = np.minimum(st, O[rows, first])          # gap through the stop fills at the open
    pnl = np.where(has & is_sl, sl_px, np.where(has, tp, C[rows, last_valid]))
    reason = np.where(has & is_sl, 0, np.where(has, 1, 2))
    return pnl.astype(np.float64), reason


def simulate(P, tr, cfg, spread):
    n = len(tr)
    one = np.ones(n)
    sig_sl, sig_tp = tr.sig_stop_d.values, tr.sig_tp_d.values
    # stop distance
    kind, val = cfg["sl"]
    if kind == "signal":
        sl = sig_sl.copy()
    elif kind == "cap":
        sl = np.minimum(sig_sl, val)
    else:
        sl = one * val
    sl = np.maximum(sl, 1.0)
    R = sl  # risk unit

    def dist(x):
        if x is None:
            return one * INF
        if x == "signal":
            return sig_tp.copy()
        return R * x

    be = dist(cfg["be"]); hold = cfg["hold_h"] * 60.0
    act = dist(cfg["trail"][0]) if cfg["trail"] else one * INF
    trail = R * cfg["trail"][1] if cfg["trail"] else one * INF
    if cfg["tp1"] is None:   # single leg
        pnl, why = sim_leg(P, sl, dist(cfg["tp2"]), be, act, trail, hold)
    else:
        f = cfg["frac1"]
        p1, w1 = sim_leg(P, sl, dist(cfg["tp1"]), be, one * INF, one * INF, hold)
        # once TP1 is hit, the runner's stop goes to breakeven at the latest
        be2 = np.minimum(be, dist(cfg["tp1"]))
        p2, why = sim_leg(P, sl, dist(cfg["tp2"]), be2, act, trail, hold)
        pnl = f * p1 + (1 - f) * p2
    return pnl / R, pnl, why


# --------------------------------------------------------------------------- grid
def grid():
    sls = [("signal", None), ("cap", 6), ("cap", 8), ("cap", 10),
           ("fixed", 4), ("fixed", 6), ("fixed", 8), ("fixed", 10), ("fixed", 12), ("fixed", 15), ("fixed", 20)]
    exits = []
    for tp in ["signal", 0.5, 0.75, 1, 1.5, 2, 3]:                       # single target
        exits.append(dict(tp1=None, frac1=0, tp2=tp, trail=None))
    for tp1, tp2 in [(0.5, 1.5), (0.5, 2), (0.5, 3), (1, 2), (1, 3), (1, "signal")]:  # 50/50 split
        exits.append(dict(tp1=tp1, frac1=0.5, tp2=tp2, trail=None))
    for act, d in [(0.5, 0.5), (1, 0.5), (1, 1), (1.5, 1), (2, 1)]:      # trailing runner / whole
        exits.append(dict(tp1=None, frac1=0, tp2=None, trail=(act, d)))
        exits.append(dict(tp1=0.5, frac1=0.5, tp2=None, trail=(act, d)))
        exits.append(dict(tp1=1, frac1=0.5, tp2=None, trail=(act, d)))
    out = []
    for sl, ex, be, hold in itertools.product(sls, exits, [None, 0.5, 1], [2, 8, 24, 120]):
        first_tp = ex["tp1"] if ex["tp1"] is not None else ex["tp2"]
        if be is not None and isinstance(first_tp, (int, float)) and be >= first_tp:
            continue
        if be is not None and ex["trail"] and be >= ex["trail"][0]:
            continue
        out.append(dict(sl=sl, be=be, hold_h=hold, **ex))
    return out


def label(c):
    sl = {"signal": "SL=signal", "cap": f"SL=min(signal,${c['sl'][1]})", "fixed": f"SL=${c['sl'][1]}"}[c["sl"][0]]
    def r(x): return "sigTP" if x == "signal" else f"{x}R"
    if c["tp1"] is not None:
        ex = f"50%@{r(c['tp1'])} + " + (f"trail {c['trail'][1]}R after {c['trail'][0]}R" if c["trail"] else f"rest@{r(c['tp2'])}")
    elif c["trail"]:
        ex = f"trail {c['trail'][1]}R after {c['trail'][0]}R"
    else:
        ex = f"TP@{r(c['tp2'])}"
    be = f"BE@{c['be']}R" if c["be"] is not None else "noBE"
    return f"{sl} | {ex} | {be} | hold {c['hold_h']}h"


def stats(r: np.ndarray) -> dict:
    n = len(r)
    if n < 2:
        return dict(n=n, mean=np.nan, t=np.nan, sum=np.nan, win=np.nan, pf=np.nan, mdd=np.nan)
    eq = np.cumsum(r)
    mdd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq)) if n else 0
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return dict(n=n, mean=float(r.mean()), t=float(r.mean() / (r.std(ddof=1) / np.sqrt(n))), sum=float(r.sum()),
                win=float((r > 0).mean()), pf=float(pos / neg) if neg > 0 else np.inf, mdd=mdd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("signals"); ap.add_argument("data_dir")
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--out", default="results/rm")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    sig = clean(pd.read_csv(a.signals))
    bars = load_bars(a.data_dir, sig["posted"].min())
    configs = grid()
    rows, keep = [], {}
    for model in ("touch", "market"):
        tr = fills(sig, bars, model, a.spread).sort_values("posted").reset_index(drop=True)
        P = path_matrix(tr, bars, a.spread)
        cut = tr.posted.iloc[len(tr) // 2]
        train = (tr.posted < cut).values
        print(f"{model}: {len(tr)} trades, train < {cut:%Y-%m-%d} ({train.sum()}), test {(~train).sum()}", flush=True)
        for k, c in enumerate(configs):
            r, pnl, why = simulate(P, tr, c, a.spread)
            s_all, s_tr, s_te = stats(r), stats(r[train]), stats(r[~train])
            key = f"{model}#{k}"
            keep[key] = (r, pnl, tr.posted.values, tr.id.values)
            rows.append(dict(key=key, entry=model, label=label(c), cfg=json.dumps(c),
                             **{f"all_{x}": v for x, v in s_all.items()},
                             **{f"tr_{x}": v for x, v in s_tr.items()},
                             **{f"te_{x}": v for x, v in s_te.items()},
                             pips_all=float(pnl.sum() * 10), stop_rate=float((why == 0).mean())))
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(a.out, "grid.csv"), index=False)
    np.savez_compressed(os.path.join(a.out, "trades.npz"),
                        **{k.replace("#", "_"): v[0] for k, v in keep.items()})
    print(f"{len(df)} configs evaluated -> {a.out}/grid.csv")


if __name__ == "__main__":
    main()
