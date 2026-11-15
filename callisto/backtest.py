"""Backtest the channel's signals over the last N days.

Pipeline: scrape t.me/s/<channel> -> classify every post with OpenAI (cached) -> fold posts
into trade ideas (earliest post wins) -> replay each idea on Dukascopy M1 bid/ask bars.

Entry model ("follower"): market order ENTRY_DELAY_SECONDS after the earliest post, at the
open of the next M1 bar (BUY at ask, SELL at bid). Stop, target and trailing are in pips from
that fill (SL_PIPS, TP_PIPS, TRAIL_*). Exits: BUY on bid, SELL on ask. If stop and target fall
in the same bar the stop wins (conservative). Trailing moves the stop only on bars after the
one that set the new extreme.

For comparison the same rules are also run from the channel's own stated entry at the
estimated call time ("channel" model) - the gap between the two is what the signal's age
costs a follower.

Usage:
  python backtest.py                     # last 7 days, settings from .env
  python backtest.py --days 7 --grid     # also sweep SL/TP/trailing (in-sample, see README)
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import channel
import classify
import numpy as np
import pandas as pd
import prices
import signals
from config import CFG

STALE_PIPS_DEFAULT = 150


@dataclass(frozen=True)
class Rules:
    sl: float
    tp: float
    trail_start: float
    trail_dist: float
    trail_step: float
    max_hold_h: float

    @staticmethod
    def from_cfg() -> Rules:
        return Rules(CFG.sl_pips, CFG.tp_pips, CFG.trail_start_pips, CFG.trail_distance_pips,
                     CFG.trail_step_pips, CFG.max_hold_hours)

    def label(self) -> str:
        tr = (f"trail {self.trail_start:g}/{self.trail_dist:g}/{self.trail_step:g}"
              if self.trail_start > 0 else "no trail")
        return f"SL {self.sl:g} TP {self.tp:g} {tr}"


def simulate(bars: pd.DataFrame, start: pd.Timestamp, side: str, rules: Rules, pip: float,
             entry_px: float | None = None) -> dict:
    """Walk M1 bars from the first bar at/after `start`. Returns the trade record."""
    i0 = bars.index.searchsorted(start)
    if i0 >= len(bars):
        return {"status": "no_data"}
    if bars.index[i0] - start > pd.Timedelta(minutes=30):
        return {"status": "market_closed"}
    buy = side == "BUY"
    if entry_px is None:
        entry_px = bars["ao"].iat[i0] if buy else bars["bo"].iat[i0]
    sgn = 1 if buy else -1
    sl = entry_px - sgn * rules.sl * pip
    tp = entry_px + sgn * rules.tp * pip
    best = entry_px
    end = bars.index[i0] + pd.Timedelta(hours=rules.max_hold_h)
    hi = bars["bh"].values if buy else bars["ah"].values
    lo = bars["bl"].values if buy else bars["al"].values
    op = bars["bo"].values if buy else bars["ao"].values
    cl = bars["bc"].values if buy else bars["ac"].values
    idx = bars.index
    mfe = mae = 0.0
    exit_px = reason = exit_t = None
    for i in range(i0, len(bars)):
        if idx[i] >= end:
            exit_px, reason, exit_t = cl[i - 1], "timeout", idx[i - 1]
            break
        if i > i0 and idx[i] - idx[i - 1] > pd.Timedelta(hours=6):
            exit_px, reason, exit_t = op[i], "gap_exit", idx[i]   # weekend: out at reopen
            if (buy and op[i] <= sl) or (not buy and op[i] >= sl):
                reason = "sl_gap"
            break
        h, lw = hi[i], lo[i]
        fav, adv = (h, lw) if buy else (lw, h)
        mfe = max(mfe, sgn * (fav - entry_px) / pip)
        mae = min(mae, sgn * (adv - entry_px) / pip)
        hit_sl = (lw <= sl) if buy else (h >= sl)
        hit_tp = (h >= tp) if buy else (lw <= tp)
        if hit_sl:
            # a gap through the stop fills at the open, not at the stop
            px = min(sl, op[i]) if buy else max(sl, op[i])
            locked = sgn * (sl - entry_px) > 0
            exit_px, reason, exit_t = px, ("trail" if locked else "sl"), idx[i]
            break
        if hit_tp:
            exit_px, reason, exit_t = tp, "tp", idx[i]
            break
        if rules.trail_start > 0:
            best = max(best, h) if buy else min(best, lw)
            if sgn * (best - entry_px) / pip >= rules.trail_start:
                new_sl = best - sgn * rules.trail_dist * pip
                if sgn * (new_sl - sl) / pip >= rules.trail_step:
                    sl = new_sl
    else:
        exit_px, reason, exit_t = cl[-1], "open", idx[-1]
    return {
        "status": "traded", "entry_time": idx[i0], "entry": round(float(entry_px), 3),
        "exit_time": exit_t, "exit": round(float(exit_px), 3), "reason": reason,
        "pips": round(sgn * (exit_px - entry_px) / pip, 1),
        "mfe": round(mfe, 1), "mae": round(mae, 1),
    }


def price_at(bars: pd.DataFrame, t: pd.Timestamp) -> float | None:
    i = bars.index.searchsorted(t) - 1
    if i < 0 or t - bars.index[i] > pd.Timedelta(minutes=30):
        return None
    return float((bars["bc"].iat[i] + bars["ac"].iat[i]) / 2)


def estimate_call_time(bars, idea: signals.Idea, t: pd.Timestamp) -> pd.Timestamp | None:
    """Last bar at or before the post where price traded through the stated entry."""
    if idea.stated_entry is None:
        return None
    w = bars.loc[t - pd.Timedelta(hours=CFG.group_hours * 2): t]
    hit = w[(w["bl"] <= idea.stated_entry) & (w["ah"] >= idea.stated_entry)]
    return hit.index[-1] if len(hit) else None


def annotate(ideas, bars, stale_pips: float) -> pd.DataFrame:
    rows = []
    for idea in ideas:
        t = pd.Timestamp(idea.first_ts).tz_convert("UTC").tz_localize(None)
        pip = CFG.pip(idea.ticker)
        mid = price_at(bars, t)
        sgn = 1 if idea.direction == "BUY" else -1
        age_pips = (round(sgn * (mid - idea.stated_entry) / pip, 1)
                    if mid is not None and idea.stated_entry is not None else None)
        call_t = estimate_call_time(bars, idea, t)
        if idea.image_clock_utc:
            call_t_img = pd.Timestamp(idea.image_clock_utc).tz_convert("UTC").tz_localize(None)
        else:
            call_t_img = None
        call = call_t_img or call_t
        skip = ""
        if idea.repost:
            skip = "repost"
        elif mid is None:
            skip = "market_closed"
        elif stale_pips and age_pips is not None and abs(age_pips) > stale_pips:
            skip = "stale"
        elif CFG.max_age_pips and age_pips is not None and age_pips > CFG.max_age_pips:
            skip = "too_old"
        rows.append({
            "idea": idea.first_post_id, "posted": t, "ticker": idea.ticker,
            "side": idea.direction, "stated_entry": idea.stated_entry,
            "price_at_post": round(mid, 2) if mid else None,
            "age_pips": age_pips, "pips_shown": idea.pips_at_first_sight,
            "call_time": call, "age_min": (round((t - call).total_seconds() / 60, 1)
                                           if call is not None else None),
            "n_posts": len(idea.post_ids), "skip": skip, "note": idea.note,
        })
    return pd.DataFrame(rows)


def run(ann: pd.DataFrame, bars, rules: Rules, model: str = "follower") -> pd.DataFrame:
    out = []
    for r in ann.itertuples():
        if r.skip:
            continue
        pip = CFG.pip(r.ticker)
        if model == "follower":
            start = r.posted + pd.Timedelta(seconds=CFG.entry_delay_seconds)
            res = simulate(bars, start, r.side, rules, pip)
        else:
            if r.stated_entry is None or r.call_time is None or pd.isna(r.call_time):
                continue
            res = simulate(bars, r.call_time, r.side, rules, pip, entry_px=r.stated_entry)
        out.append({"idea": r.idea, "posted": r.posted, "side": r.side, **res})
    return pd.DataFrame(out)


def by_week(t: pd.DataFrame) -> pd.DataFrame:
    t = t[t["status"] == "traded"] if len(t) else t
    if not len(t):
        return pd.DataFrame()
    wk = pd.to_datetime(t["posted"]).dt.to_period("W-SUN").dt.start_time.dt.date
    g = t.groupby(wk)
    out = g["pips"].agg(trades="count", net_pips="sum", avg="mean")
    out["win_rate"] = g["pips"].apply(lambda p: (p > 0).mean()).round(2)
    side = t.pivot_table(index=wk, columns="side", values="pips", aggfunc="sum")
    out = out.join(side.add_suffix("_pips")).round(1)
    out.index.name = "week"
    return out


def stats(t: pd.DataFrame) -> dict:
    t = t[t.get("status", pd.Series(dtype=str)) == "traded"] if len(t) else t
    if not len(t):
        return {"trades": 0}
    p = t["pips"]
    return {
        "trades": len(t), "wins": int((p > 0).sum()), "win_rate": round((p > 0).mean(), 3),
        "net_pips": round(p.sum(), 1), "avg_pips": round(p.mean(), 1),
        "t_stat": round(p.mean() / (p.std(ddof=1) / np.sqrt(len(p))), 2) if len(p) > 2 else None,
        "worst": round(p.min(), 1), "best": round(p.max(), 1),
        "usd_per_lot": round(p.sum() * CFG.lot_usd_per_pip, 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--grid", action="store_true", help="sweep SL/TP/trailing")
    ap.add_argument("--stale-pips", type=float, default=STALE_PIPS_DEFAULT,
                    help="skip ideas whose stated entry is this far from market at post time")
    ap.add_argument("--refetch", action="store_true", help="re-scrape the channel")
    ap.add_argument("--offline", action="store_true",
                    help="use cached posts only (no scrape); prices still load from cache")
    args = ap.parse_args()

    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)
    raw_path = CFG.data_dir / "posts_raw.json"
    cached = json.loads(raw_path.read_text()) if raw_path.exists() else []
    if args.offline and cached:
        posts = [channel.Post(**d) for d in cached]
    elif args.refetch or not cached or min(d["time"] for d in cached) > since.isoformat():
        posts = channel.fetch_since(CFG.channel, since)
    else:
        known = {d["id"] for d in cached}
        posts = [channel.Post(**d) for d in cached]
        posts += [p for p in channel.fetch_since(CFG.channel, datetime.fromisoformat(
            max(d["time"] for d in cached))) if p.id not in known]
    raw_path.write_text(json.dumps([p.to_dict() for p in posts], ensure_ascii=False, indent=1))
    posts = [p for p in posts if p.ts >= since]
    print(f"{len(posts)} posts {posts[0].time[:16]} .. {posts[-1].time[:16]}")

    rows = classify.classify_all(posts, classify.Cache(CFG.data_dir / "classified.jsonl"))
    kinds = pd.Series([r["llm"]["kind"] for r in rows]).value_counts().to_dict()
    print("post kinds:", kinds)
    ideas = signals.build(rows)

    tickers = sorted({i.ticker for i in ideas})
    bars_by = {tk: prices.load(tk, since - timedelta(days=1), now) for tk in tickers}
    bars = bars_by.get("XAUUSD")
    if len(tickers) > 1:
        print(f"note: only XAUUSD is replayed; also saw {tickers}")
    ideas = [i for i in ideas if i.ticker == "XAUUSD"]
    ann = annotate(ideas, bars, args.stale_pips)

    rules = Rules.from_cfg()
    fol = run(ann, bars, rules, "follower")
    chn = run(ann, bars, rules, "channel")
    out_dir = CFG.data_dir / "results"
    out_dir.mkdir(exist_ok=True)
    ann.to_csv(out_dir / "ideas.csv", index=False)
    trades = ann.merge(fol, on=["idea", "posted", "side"], how="left")
    trades.to_csv(out_dir / "trades_follower.csv", index=False)
    chn.to_csv(out_dir / "trades_channel.csv", index=False)

    pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 40)
    skipped = ann["skip"].replace("", np.nan).value_counts().to_dict()
    print(f"\n{len(ann)} ideas; skipped: {skipped}")
    cols = ["idea", "posted", "side", "stated_entry", "price_at_post", "age_pips", "age_min",
            "entry", "exit", "reason", "pips", "mfe", "mae"]
    print(trades[cols].to_string(index=False))
    print(f"\nrules: {rules.label()}, max hold {rules.max_hold_h:g}h, "
          f"entry delay {CFG.entry_delay_seconds:g}s")
    s_f, s_c = stats(fol), stats(chn)
    print("follower (enter at first post):", s_f)
    print("channel  (stated entry, est. call time):", s_c)
    weekly = by_week(fol)
    if len(weekly) > 1:
        print("\nfollower by week (Mon start):")
        print(weekly.to_string())

    grid = []
    if args.grid:
        for sl, tp, tr in itertools.product([30, 50, 80, 120, 200], [50, 100, 150, 250, 400],
                                            [(0, 0, 0), (30, 20, 10), (50, 30, 10),
                                             (100, 50, 10)]):
            r = replace(rules, sl=sl, tp=tp, trail_start=tr[0], trail_dist=tr[1],
                        trail_step=tr[2])
            s = stats(run(ann, bars, r, "follower"))
            grid.append({"sl": sl, "tp": tp, "trail": f"{tr[0]}/{tr[1]}/{tr[2]}", **s})
        g = pd.DataFrame(grid).sort_values("net_pips", ascending=False)
        g.to_csv(out_dir / "grid.csv", index=False)
        print("\ngrid (follower, in-sample - top 10 / bottom 5):")
        print(g.head(10).to_string(index=False))
        print(g.tail(5).to_string(index=False))
        print(f"grid cells with net_pips > 0: {(g['net_pips'] > 0).sum()}/{len(g)}")

    summary = {"generated": now.isoformat(), "days": args.days, "posts": len(posts),
               "post_kinds": kinds, "ideas": len(ann),
               "skipped": ann["skip"].replace("", np.nan).value_counts().to_dict(),
               "rules": rules.__dict__, "follower": s_f, "channel": s_c,
               "weekly": {str(k): v for k, v in weekly.to_dict("index").items()}}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
