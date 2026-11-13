"""Replay Tickmind AI XAUUSD signals on HistData M1 bars.

HistData bid bars; these files are labelled in New York local time (DST-aware).
Each signal is replayed from its posting time (postedAtUnix, UTC) with the
stated stop and target. Same-bar SL+TP ambiguity resolves to SL (conservative).

Fill models:
  market : enter at the open of the first full minute after the post
  touch  : pending order at the stated entry; fills when price trades through it
           within LIMIT_WAIT, from whichever side the market is on at post time
Management variants:
  fixed  : stated SL / TP only
  be1r   : move stop to entry once price reaches +1R

Usage: python backtest_signals.py signals_raw.csv /path/to/XAUUSD [--spread 0.30]
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

LIMIT_WAIT = pd.Timedelta(hours=4)
MAX_HOLD = pd.Timedelta(days=5)
MAX_POST_GAP = pd.Timedelta(minutes=30)   # first bar must start within this of the post
MAX_BAR_GAP = pd.Timedelta(days=3)        # longer than a weekend = hole in the data


def load_bars(data_dir: str, start: pd.Timestamp) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(data_dir, "DAT_ASCII_XAUUSD_M1_2026*.csv")))
    frames = []
    for f in files:
        df = pd.read_csv(f, sep=";", header=None, names=["ts", "o", "h", "l", "c", "v"])
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    # These files are labelled in New York local time WITH daylight saving: the daily
    # gold break ends at 18:00 on the label all year (verified Jan-Nov 2026). A fixed
    # UTC-5 shift puts every Mar-Nov bar one hour late.
    ts = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S")
    ts = ts.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
    df["ts"] = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.dropna(subset=["ts"]).drop_duplicates("ts").set_index("ts").sort_index()
    return df.loc[start - pd.Timedelta(days=1):, ["o", "h", "l", "c"]]


def clean(sig: pd.DataFrame) -> pd.DataFrame:
    sig = sig.copy()
    sig["note"] = ""
    bad_tp = sig["target"] > 10 * sig["entry"]
    sig.loc[bad_tp, "target"] = sig.loc[bad_tp, "target"] / 100
    sig.loc[bad_tp, "note"] = "target typo /100"
    risk = (sig["entry"] - sig["stop"]).abs()
    sig.loc[(risk < 2) & (sig["status"] == "closed"), "note"] = "stop too tight (typo?) - excluded"
    sig["risk"] = risk
    sig["posted"] = pd.to_datetime(sig["posted_unix"], unit="s")
    return sig


def manage(bars, i0, side, entry, stop, target, risk, be1r, spread):
    """Walk bars from index i0. Returns (exit_price, reason, exit_time)."""
    end_t = bars.index[i0] + MAX_HOLD
    h, l, c, idx = bars["h"].values, bars["l"].values, bars["c"].values, bars.index
    sl = stop
    for i in range(i0, len(bars)):
        if i > i0 and idx[i] - idx[i - 1] > MAX_BAR_GAP:
            return np.nan, "data_gap", idx[i - 1]
        if idx[i] > end_t:
            return c[i - 1], "timeout", idx[i - 1]
        if side == "BUY":   # long: exits on bid
            hit_sl, hit_tp = l[i] <= sl, h[i] >= target
        else:               # short: exits on ask = bid + spread
            hit_sl, hit_tp = h[i] + spread >= sl, l[i] + spread <= target
        if hit_sl:
            return sl, ("be" if sl == entry else "sl"), idx[i]
        if hit_tp:
            return target, "tp", idx[i]
        if be1r:
            if side == "BUY" and h[i] >= entry + risk:
                sl = max(sl, entry)
            if side == "SELL" and l[i] + spread <= entry - risk:
                sl = min(sl, entry)
    return c[-1], "open_at_data_end", idx[-1]


def run(sig, bars, fill, be1r, spread):
    out = []
    data_end = bars.index[-1]
    for _, s in sig.iterrows():
        rec = dict(id=s.id, posted=s.posted, side=s.side, entry=s.entry, stop=s.stop,
                   target=s.target, claimed_pips=s.result_pips, note=s.note)
        if s.status != "closed" or "excluded" in s.note:
            out.append({**rec, "outcome": "skipped"}); continue
        if s.posted >= data_end:
            out.append({**rec, "outcome": "no_data"}); continue
        i0 = bars.index.searchsorted(s.posted.ceil("min"))
        if i0 >= len(bars) or bars.index[i0] - s.posted > MAX_POST_GAP:
            out.append({**rec, "outcome": "no_data"}); continue
        px_at_post = bars["o"].iat[i0]
        rec["px_at_post"] = px_at_post
        # + = market already past the entry in the trade's direction at post time
        rec["entry_gap"] = (px_at_post - s.entry) * (1 if s.side == "BUY" else -1)
        if fill == "market":
            past = (px_at_post <= s.stop or px_at_post >= s.target) if s.side == "BUY" \
                else (px_at_post >= s.stop or px_at_post <= s.target)
            if past:  # stop or target already traded through before the post: nothing to enter
                out.append({**rec, "outcome": "past_level_at_post"}); continue
            fill_px = px_at_post + (spread if s.side == "BUY" else 0)
            i_fill = i0
        else:
            win = bars.iloc[i0:].loc[: s.posted + LIMIT_WAIT]
            # pending order at the stated entry: fills only once price trades through it
            # from whichever side the market is on at post time (bid series, ask = bid + spread)
            off = spread if s.side == "BUY" else 0.0
            if px_at_post + off > s.entry:
                touched = np.flatnonzero(win["l"].values + off <= s.entry)
            else:
                touched = np.flatnonzero(win["h"].values + off >= s.entry)
            if len(touched) == 0:
                out.append({**rec, "outcome": "not_filled"}); continue
            i_fill = i0 + touched[0]
            fill_px = s.entry
        exit_px, reason, t_exit = manage(bars, i_fill, s.side, fill_px, s.stop, s.target,
                                         s.risk, be1r, spread)
        if reason == "data_gap":
            out.append({**rec, "outcome": "data_gap"}); continue
        pnl = (exit_px - fill_px) if s.side == "BUY" else (fill_px - exit_px)
        out.append({**rec, "outcome": reason, "fill_time": bars.index[i_fill], "fill_px": fill_px,
                    "exit_time": t_exit, "exit_px": exit_px, "pnl_usd": pnl,
                    "pnl_pips": pnl * 10, "R": pnl / s.risk})
    return pd.DataFrame(out)


def summarize(name, df):
    t = df[df["R"].notna()] if "R" in df else df.iloc[0:0]
    if t.empty:
        return f"{name}: no trades"
    r = t["R"]
    se = r.std(ddof=1) / np.sqrt(len(r))
    counts = t["outcome"].value_counts().to_dict()
    skipped = {k: v for k, v in df["outcome"].value_counts().items() if k in ("no_data", "data_gap", "not_filled", "past_level_at_post")}
    return (f"{name:16s} n={len(r):3d}  win={np.mean(r > 0):5.1%}  meanR={r.mean():+.3f}"
            f" (t={r.mean() / se:+.2f})  sumR={r.sum():+6.1f}  pips={t['pnl_pips'].sum():+7.0f}  {counts}  skipped={skipped}")


SITE_LOSS_PIPS = -50   # the site's netPips books every stop-out at -50 pips (verified 2026-11-13)
MODELS = [("market", False), ("market", True), ("touch", False), ("touch", True)]
MODEL_LABEL = {
    "market_fixed": "Market fill at post, fixed SL/TP",
    "market_be1r": "Market fill at post, BE at +1R",
    "touch_fixed": "Order at stated entry, fixed SL/TP",
    "touch_be1r": "Order at stated entry, BE at +1R",
}


def site_view(sig: pd.DataFrame) -> pd.DataFrame:
    """What the site says happened, per closed signal."""
    c = sig[sig.status == "closed"].copy()
    c["site_win"] = c["result_pips"].notna()
    c["site_pips"] = c["result_pips"].fillna(SITE_LOSS_PIPS)
    c["target_pips"] = (c["target"] - c["entry"]).abs() * 10
    c["risk_pips"] = c["risk"] * 10
    return c


def model_stats(t: pd.DataFrame) -> dict:
    r = t["R"]
    n = len(r)
    se = r.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return dict(n=n, win_rate=float((r > 0).mean()), mean_R=float(r.mean()), t_stat=float(r.mean() / se),
                sum_R=float(r.sum()), net_pips=float(t["pnl_pips"].sum()),
                outcomes={k: int(v) for k, v in t["outcome"].value_counts().items()})


def build_summary(sig, bars, trades: dict, spread: float) -> dict:
    site = site_view(sig)
    base = trades["touch_fixed"]
    tested_ids = set(base.loc[base["R"].notna(), "id"])
    st = site[site.id.isin(tested_ids)]
    wins = site[site.site_win]

    summary = {
        "generated_utc": pd.Timestamp.now("UTC").isoformat(timespec="seconds"),
        "spread_usd": spread,
        "signals_total": int(len(sig)),
        "signals_closed": int(len(site)),
        "signals_tested": int(len(tested_ids)),
        "bars_range": [str(bars.index[0]), str(bars.index[-1])],
        "signals_range": [str(sig.posted.min()), str(sig.posted.max())],
        "data_notes": [f"{n.id}: {n.note}" for n in sig.loc[sig.note != "", ["id", "note"]].itertuples()],
        "site_all": dict(n=int(len(site)), win_rate=float(site.site_win.mean()),
                         net_pips=float(site.site_pips.sum())),
        "site_tested": dict(n=int(len(st)), win_rate=float(st.site_win.mean()),
                            net_pips=float(st.site_pips.sum())),
        "accounting": dict(
            site_loss_pips=SITE_LOSS_PIPS,
            median_stop_pips=float(site.risk_pips.median()),
            median_target_pips=float(site.target_pips.median()),
            wins_claiming_beyond_target=int((wins.site_pips > wins.target_pips + 1).sum()),
            wins_total=int(len(wins)),
            median_claimed_win_pips=float(wins.site_pips.median()),
        ),
        "models": {},
    }
    for key, df in trades.items():
        t = df[df["R"].notna()]
        summary["models"][key] = {"label": MODEL_LABEL[key], **model_stats(t)}

    # per-signal comparison against the primary model
    m = base.merge(site[["id", "site_win", "site_pips", "target_pips", "risk_pips"]], on="id", how="left")
    m = m[m["R"].notna()].copy()
    m["bt_win"] = m["R"] > 0
    summary["agreement"] = {
        "site_win_bt_win": int((m.site_win & m.bt_win).sum()),
        "site_win_bt_loss": int((m.site_win & ~m.bt_win).sum()),
        "site_loss_bt_win": int((~m.site_win & m.bt_win).sum()),
        "site_loss_bt_loss": int((~m.site_win & ~m.bt_win).sum()),
    }

    # disputed trades: site says win, replay stopped out. Did price reach TP only after the stop?
    disp = m[m.site_win & (m.outcome == "sl")]
    after = 0
    for _, r in disp.iterrows():
        w = bars.loc[pd.Timestamp(r.exit_time): pd.Timestamp(r.fill_time) + MAX_HOLD]
        after += bool((w.h.max() >= r.target) if r.side == "BUY" else (w.l.min() + spread <= r.target))
    mins = (pd.to_datetime(disp.exit_time) - pd.to_datetime(disp.fill_time)).dt.total_seconds() / 60
    summary["disputed"] = dict(n=int(len(disp)), tp_after_stop=int(after),
                               median_minutes_to_stop=float(mins.median()) if len(mins) else None)
    mk = trades["market_fixed"]
    summary["past_level_at_post"] = int((mk.outcome == "past_level_at_post").sum())

    # price move in the signal's direction during the 30 min BEFORE the post, and 2 h after it
    c, idx = bars["c"], bars.index
    pre, post = [], []
    for _, s in sig[sig.status == "closed"].iterrows():
        i0 = idx.searchsorted(s.posted.ceil("min"))
        if i0 >= len(bars) or idx[i0] - s.posted > MAX_POST_GAP:
            continue
        sg = 1 if s.side == "BUY" else -1
        tb, ta = s.posted - pd.Timedelta(minutes=30), s.posted + pd.Timedelta(minutes=120)
        jb = min(idx.searchsorted(tb), len(c) - 1)
        ja = min(idx.searchsorted(ta), len(c) - 1)
        if abs((idx[jb] - tb).total_seconds()) < 900:
            pre.append(sg * (c.iat[i0] - c.iat[jb]))
        if abs((idx[ja] - ta).total_seconds()) < 900:
            post.append(sg * (c.iat[ja] - c.iat[i0]))
    pre, post = np.array(pre), np.array(post)
    summary["timing"] = dict(pre30_mean_usd=float(pre.mean()), pre30_share_with=float((pre > 0).mean()),
                             post120_mean_usd=float(post.mean()), post120_share_with=float((post > 0).mean()),
                             n=int(len(pre)))
    summary["primary_model"] = "touch_fixed"

    # cumulative pips through time: site claim vs each model
    curves = {"site": []}
    for _, r in m.sort_values("posted").iterrows():
        curves["site"].append([str(r.posted), float(r.site_pips)])
    for key, df in trades.items():
        t = df[df["R"].notna()].sort_values("posted")
        curves[key] = [[str(p), float(x)] for p, x in zip(t.posted, t.pnl_pips)]
    summary["curves"] = curves

    # monthly table
    m["month"] = pd.to_datetime(m["posted"]).dt.strftime("%Y-%m")
    monthly = []
    for mo, g in m.groupby("month"):
        tt = trades["touch_fixed"]
        tg = tt[(tt["R"].notna()) & (pd.to_datetime(tt["posted"]).dt.strftime("%Y-%m") == mo)]
        monthly.append(dict(month=mo, n=int(len(g)), site_win_rate=float(g.site_win.mean()),
                            site_pips=float(g.site_pips.sum()), bt_win_rate=float(g.bt_win.mean()),
                            bt_pips=float(g.pnl_pips.sum()), bt_R=float(g.R.sum()),
                            touch_pips=float(tg.pnl_pips.sum())))
    summary["monthly"] = monthly

    rows = []
    for _, r in m.sort_values("posted", ascending=False).iterrows():
        rows.append(dict(id=r.id, posted=str(r.posted), side=r.side, entry=r.entry, stop=r.stop,
                         target=r.target, risk_pips=round(float(r.risk_pips)),
                         site="WIN" if r.site_win else "LOSS", site_pips=float(r.site_pips),
                         bt=r.outcome.upper(), bt_pips=round(float(r.pnl_pips)), bt_R=round(float(r.R), 2),
                         entry_gap=round(float(r.entry_gap), 2)))
    untested = site[~site.id.isin(tested_ids)]
    summary["untested"] = [dict(id=r.id, posted=str(r.posted), site_pips=float(r.site_pips))
                           for _, r in untested.iterrows()]
    summary["signals"] = rows
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("signals")
    ap.add_argument("data_dir")
    ap.add_argument("--spread", type=float, default=0.30, help="USD per oz round-trip spread")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    sig = clean(pd.read_csv(a.signals))
    bars = load_bars(a.data_dir, sig["posted"].min())
    print(f"signals {len(sig)}  ({sig.posted.min()} .. {sig.posted.max()} UTC)")
    print(f"bars    {bars.index[0]} .. {bars.index[-1]} UTC")
    for n in sig.loc[sig.note != "", ["id", "note"]].itertuples():
        print(f"  note: {n.id} {n.note}")
    os.makedirs(a.out, exist_ok=True)
    trades = {}
    for fill, be in MODELS:
        key = f"{fill}_{'be1r' if be else 'fixed'}"
        trades[key] = run(sig, bars, fill, be, a.spread)
        trades[key].to_csv(os.path.join(a.out, f"trades_{key}.csv"), index=False)
        print(summarize(key, trades[key]))
    summ = build_summary(sig, bars, trades, a.spread)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summ, f, indent=1, default=str)
    print(f"\nsite (all closed): {summ['site_all']}")
    print(f"site (tested)    : {summ['site_tested']}")
    print(f"accounting       : {summ['accounting']}")
    print(f"agreement        : {summ['agreement']}")
    print(f"\nwrote {a.out}/summary.json and per-trade CSVs")


if __name__ == "__main__":
    main()
