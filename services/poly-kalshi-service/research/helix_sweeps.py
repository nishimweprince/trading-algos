"""PLAN 3.3 on the Helix tape: the sweeps, the null battery and the price-side latency curve.

    python -m research.helix_sweeps --split discovery     # parameters are chosen here
    python -m research.helix_sweeps --split earlier       # the spot regime: reported only
    python -m research.helix_sweeps --split holdout       # refuses until research/PREREG.md exists

Reads ``research/data/helix/<day>/`` (``research.helix_replay``), the venue
resolutions and the inventory's day roles. Writes ``helix_sweeps_<split>.json``.

Everything is net of the fee and settled on Polymarket's own outcome (Gamma).
"At the touch" means the ask at the decision; the price-side latency curve then
asks whether that ask was still at or below the limit L ms later, using the
tick-level slices. Sizes on this tape are the touch size only.

- ``variants``: per variant, entries, entries a day, win rate, average price,
  net ¢/share at the touch with its s.e., day-t, days and assets positive, per asset.
- ``thresholds``: the uncensored first crossing per threshold (``windows.parquet``):
  how often each fires.
- ``nulls``: ``research.nulls`` on the base rule, overall and per asset.
- ``latency``: per latency and limit offset, the share of entries whose cheap ask
  was still at or below the limit, the net at that ask, and the trade-through proxy
  (a buy at or through our ask between the crossing and the order).
- ``spreads``: net by the Polymarket and Kalshi spread at the crossing.
- ``sizing``: daily P&L s.d. for $50 notional, 100 shares and min($50, touch).
- ``regime``: the questions epoch 4 raised (0 wins in 16, 07:15-08:15 UTC; PLAN
  after epoch 4, A1-A3 and A5), on the base rule:
  - ``bad_hours``: per clock hour with ≥ 10 entries, how often none won and how
    often the hour's net was as bad as epoch 4's −31¢ (A1);
  - ``by_hour_utc``: net, win rate and median Kalshi spread per UTC hour, and
    the US night (``shadow.US_NIGHT_UTC``) against the day (A2);
  - ``kalshi_extreme``: entries where Kalshi's mid is further from 0.5 than
    Polymarket's, against the rest (A3), and the floor at 0.15;
  - ``clustering``: entries per window, the window-clustered s.e., and one
    entry per window (the largest |gap|) against all (A5).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from poly_kalshi_service.fees import taker_fee
from poly_kalshi_service.shadow import US_NIGHT_UTC
from research.nulls import arms, summarise

LATENCIES_MS = (0, 25, 50, 100, 150, 250, 400)
OFFSETS_C = (0, 1, 2, 3)
PREREG = Path(__file__).with_name("PREREG.md")


def _fee(px: pd.Series) -> pd.Series:
    return px.map(lambda p: taker_fee(p, 1.0))


def _t(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return None if sd == 0 else statistics.fmean(values) / (sd / math.sqrt(len(values)))


def _stats(df: pd.DataFrame, days: int) -> dict[str, Any]:
    if df.empty:
        return {"n": 0}
    daily = df.groupby("day")["net"].mean().tolist()
    by_asset = df.groupby("asset")["net"].mean()
    return {
        "n": int(len(df)),
        "per_day": round(len(df) / max(days, 1), 1),
        "win_rate": round(float(df["win"].mean()), 4),
        "avg_px": round(float(df["entry_px"].mean()), 4),
        "edge_pp": round(100 * float(df["win"].mean() - df["entry_px"].mean()), 2),
        "net_cents": round(float(df["net"].mean()), 2),
        "se": round(float(df["net"].std(ddof=1) / math.sqrt(len(df))), 2) if len(df) > 1 else None,
        "day_t": None if (t := _t(daily)) is None else round(t, 2),
        "days_positive": f"{sum(1 for d in daily if d > 0)}/{len(daily)}",
        "assets_positive": f"{int((by_asset > 0).sum())}/{len(by_asset)}",
        "touch_usd_median": round(float((df["touch_size"] * df["entry_px"]).median()), 2),
        "by_asset": {
            a: {"n": int(len(g)), "net_cents": round(float(g["net"].mean()), 2),
                "win_rate": round(float(g["win"].mean()), 3),
                "touch_usd_median": round(float((g["touch_size"] * g["entry_px"]).median()), 2)}
            for a, g in df.groupby("asset")
        },
    }  # fmt: skip


def load(data: Path, days: list[str]) -> dict[str, pd.DataFrame]:
    parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    for day in days:
        d = data / day
        if not (d / "summary.json").is_file():
            continue
        for name in ("crossings", "windows"):
            df = pd.read_parquet(d / f"{name}.parquet")
            df["day"] = day
            parts[name].append(df)
    return {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}


def settle(cross: pd.DataFrame, res: pd.DataFrame) -> pd.DataFrame:
    df = cross.merge(res[["asset", "open_unix", "poly_outcome"]], on=["asset", "open_unix"])
    df = df[df["poly_outcome"].notna()].copy()
    df["win"] = (df["side"] == df["poly_outcome"]).astype(float)
    df["net"] = 100 * (df["win"] - df["entry_px"] - _fee(df["entry_px"]))
    return df


def variants(df: pd.DataFrame, days: int) -> dict[str, Any]:
    entries = df[df["enter"]]
    out = {}
    for name, g in entries.groupby("variant"):
        out[name] = _stats(g, days) | {
            "crossings": int((df["variant"] == name).sum()),
            "skips": int(((df["variant"] == name) & ~df["enter"]).sum()),
        }
    return out


def thresholds(windows: pd.DataFrame) -> dict[str, Any]:
    n = len(windows)
    out = {"windows": n}
    for col in [c for c in windows.columns if c.startswith("first_cross_s_")]:
        key = "0." + col.rsplit("_", 1)[1]
        fired = windows[col].notna()
        out[key] = {
            "fires_share": round(float(fired.mean()), 4),
            "median_first_s": None if not fired.any() else round(float(windows[col].median()), 1),
        }
    full = windows["max_abs_gap_full"].dropna()
    out["max_abs_gap_full_quantiles"] = {
        f"p{q}": round(float(full.quantile(q / 100)), 3) for q in (10, 25, 50, 75, 90, 99)
    }
    return out


def nulls(data: Path, days: list[str], cross: pd.DataFrame, res: pd.DataFrame) -> dict[str, Any]:
    base = cross[cross["variant"] == "base"]
    books: dict[str, list[pd.DataFrame]] = defaultdict(list)
    for day in days:
        gpath = data / day / "grid.parquet"
        if not gpath.is_file():
            continue
        grid = pd.read_parquet(gpath)
        d = base[base["day"] == day]
        r = res[res["day"] == day]
        journal = [
            {"type": "decision", "asset": x.asset, "open_unix": int(x.open_unix), "side": x.side,
             "entry_px": x.entry_px}
            for x in d.itertuples()
        ] + [
            {"type": "resolution", "venue": "poly", "asset": x.asset,
             "open_unix": int(x.open_unix), "outcome": x.poly_outcome}
            for x in r.itertuples() if x.poly_outcome
        ]  # fmt: skip
        for name, book in arms(grid, journal, cap=0.60, seed=int(day)).items():
            books[name].append(book)
    merged = {k: pd.concat(v, ignore_index=True) for k, v in books.items() if v}
    if not merged:
        return {}
    out = summarise(merged)
    s, n = merged["signal"], merged["random_same_rule"]
    excess = 100 * (s["win"].mean() - s["px"].mean()) - 100 * (n["win"].mean() - n["px"].mean())
    se = math.sqrt(
        (s["gross_cents"].var(ddof=1) / len(s)) + (n["gross_cents"].var(ddof=1) / len(n))
    )
    out["signal_minus_random_same_rule_pp"] = {"excess": round(float(excess), 2),
                                               "se": round(float(se), 2)}  # fmt: skip
    return out


def _latency_rows(data: Path, days: list[str], entries: pd.DataFrame) -> pd.DataFrame:
    """Per entry (every variant): the cheap ask and its size L ms after the crossing,
    and whether a buy traded at or through our ask by then."""
    rows = []
    lats = np.array(LATENCIES_MS, dtype=float)
    for day in days:
        spath = data / day / "slices.parquet"
        if not spath.is_file():
            continue
        sl = pd.read_parquet(spath, columns=["asset", "open_unix", "t_cross", "kind", "dt_ms",
                                             "ask", "asz", "trade_px"])  # fmt: skip
        sl = sl[sl["dt_ms"] >= 0].sort_values("dt_ms")
        legs: dict[tuple[Any, ...], tuple[np.ndarray, ...]] = {}
        for key, g in sl.groupby(["asset", "open_unix", "t_cross", "kind"], sort=False):
            legs[key] = (g["dt_ms"].to_numpy(), g["ask"].to_numpy(), g["asz"].to_numpy(),
                         g["trade_px"].to_numpy())  # fmt: skip
        for x in entries[entries["day"] == day].itertuples():
            leg = "y" if x.side == "yes" else "n"
            bbo = legs.get((x.asset, x.open_unix, x.t_ns, f"bbo_{leg}"))
            trades = legs.get((x.asset, x.open_unix, x.t_ns, f"trade_{leg}"))
            row = {"variant": x.variant, "asset": x.asset, "day": day, "win": x.win,
                   "px": x.entry_px}  # fmt: skip
            if bbo is not None:
                i = np.searchsorted(bbo[0], lats, side="right") - 1
                asks = np.where(i >= 0, bbo[1][np.maximum(i, 0)], x.entry_px)
                sizes = np.where(i >= 0, bbo[2][np.maximum(i, 0)], x.touch_size)
            else:  # no update within 5 s: the touch stood
                asks, sizes = np.full(len(lats), x.entry_px), np.full(len(lats), x.touch_size)
            for k, lat in enumerate(LATENCIES_MS):
                row[f"ask_{lat}"] = float(asks[k])
                row[f"asz_{lat}"] = float(sizes[k])
                row[f"thru_{lat}"] = bool(
                    trades is not None
                    and ((trades[0] <= lat) & (trades[3] >= x.entry_px - 1e-9)).any()
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _fill(df: pd.DataFrame, lat: int, off: int) -> tuple[pd.Series, pd.Series]:
    """Whether the order (limit = touch + off ¢, at most the cap) found the ask, and the
    net ¢/share at that ask, after the fee (NaN where it did not)."""
    limit = np.minimum(df["px"] + off / 100, 0.60)
    ask = df[f"ask_{lat}"]
    ok = (ask > 0) & (ask <= limit + 1e-9)
    net = 100 * (df["win"] - ask - ask.map(lambda p: taker_fee(p, 1.0)))
    return ok, net.where(ok)


def latency(data: Path, days: list[str], entries: pd.DataFrame) -> dict[str, Any]:
    """Price side on the slices: was the cheap ask still at or below the limit after L ms?"""
    df = _latency_rows(data, days, entries)
    if df.empty:
        return {}
    base = df[df["variant"] == "base"]
    touch_net = 100 * (base["win"] - base["px"] - base["px"].map(lambda p: taker_fee(p, 1.0)))
    out: dict[str, Any] = {"entries": int(len(base))}
    for lat in LATENCIES_MS:
        for off in OFFSETS_C:
            ok, net = _fill(base, lat, off)
            out[f"{lat}ms+{off}c"] = {
                "fill_share": round(float(ok.mean()), 4),
                "net_cents_filled": None if not ok.any() else round(float(net[ok].mean()), 2),
                "net_cents_missed_at_touch": None
                if ok.all()
                else round(float(touch_net[~ok].mean()), 2),
                "net_cents_per_entry": round(float(net.fillna(0).mean()), 2),
                "touch_usd_median": None
                if not ok.any()
                else round(float((base[f"asz_{lat}"] * base[f"ask_{lat}"])[ok].median()), 2),
                "by_asset_fill_share": {
                    a: round(float(ok[base["asset"] == a].mean()), 3)
                    for a in sorted(base["asset"].unique())
                },
            }
    for lat in LATENCIES_MS:
        t = base[f"thru_{lat}"]
        out[f"trade_through_{lat}ms"] = {
            "share": round(float(t.mean()), 4),
            "win_rate_if_through": None if not t.any() else round(float(base[t]["win"].mean()), 4),
            "win_rate_if_not": None if t.all() else round(float(base[~t]["win"].mean()), 4),
        }
    ndays = max(base["day"].nunique(), 1)
    per_variant = {}
    for name, g in df.groupby("variant"):
        cells = {}
        for off in (0, 2):
            ok, net = _fill(g, 150, off)
            per_entry = float(net.fillna(0).mean())
            cells[f"150ms+{off}c"] = {
                "fill_share": round(float(ok.mean()), 4),
                "net_cents_filled": None if not ok.any() else round(float(net[ok].mean()), 2),
                "net_cents_per_entry": round(per_entry, 2),
                "net_cents_per_day": round(per_entry * len(g) / ndays, 0),
            }
        per_variant[name] = cells
    out["variants_150ms"] = per_variant
    out["prereg"] = prereg_comparisons(df)
    return out


def _paired(a: pd.Series, b: pd.Series) -> dict[str, Any]:
    """Day-by-day difference a − b (both indexed by day): mean, day-t, days positive."""
    d = (a - b).dropna()
    t = _t(d.tolist())
    return {
        "mean_daily_diff": round(float(d.mean()), 2),
        "day_t": None if t is None else round(t, 2),
        "days_positive": f"{int((d > 0).sum())}/{len(d)}",
    }


def prereg_comparisons(df: pd.DataFrame) -> dict[str, Any]:
    """research/PREREG.md C1 and C2, at 150 ms, price side; an entry the limit misses is 0."""

    def per_entry(variant: str, off: int) -> pd.DataFrame:
        g = df[df["variant"] == variant]
        _ok, net = _fill(g, 150, off)
        return pd.DataFrame({"day": g["day"], "net": net.fillna(0)})

    base0, base2, t15 = per_entry("base", 0), per_entry("base", 2), per_entry("t15", 0)
    c1 = _paired(base2.groupby("day")["net"].mean(), base0.groupby("day")["net"].mean())
    c1 |= {"per_entry_plus2c": round(float(base2["net"].mean()), 2),
           "per_entry_touch": round(float(base0["net"].mean()), 2)}  # fmt: skip
    c2 = _paired(t15.groupby("day")["net"].sum(), base0.groupby("day")["net"].sum())
    c2 |= {"per_day_t15": round(float(t15.groupby("day")["net"].sum().mean()), 0),
           "per_day_base": round(float(base0.groupby("day")["net"].sum().mean()), 0)}  # fmt: skip
    return {"C1_offset_plus2c_vs_touch": c1, "C2_t15_vs_t10_per_day": c2}


def spreads(entries: pd.DataFrame) -> dict[str, Any]:
    base = entries[entries["variant"] == "base"].copy()
    base["p_spread"] = np.where(
        base["side"] == "yes", base["y_ask"] - base["y_bid"], base["n_ask"] - base["n_bid"]
    )
    base["k_spread"] = base["k_ask"] - base["k_bid"]
    out = {}
    for col, bins in (("p_spread", [0, 0.015, 0.025, 0.05, 0.1, 1]),
                      ("k_spread", [0, 0.015, 0.025, 0.05, 0.1, 1])):  # fmt: skip
        cut = pd.cut(base[col], bins, include_lowest=True)
        out[col] = {
            str(k): {"n": int(len(g)), "net_cents": round(float(g["net"].mean()), 2)}
            for k, g in base.groupby(cut, observed=True)
        }
    return out


def sizing(entries: pd.DataFrame) -> dict[str, Any]:
    base = entries[entries["variant"] == "base"].copy()
    per_share = (base["net"] / 100).to_numpy()
    plans = {
        "notional_50": np.floor(50 / base["entry_px"]),
        "fixed_100": pd.Series(100.0, index=base.index),
        "min_50_touch": np.minimum(np.floor(50 / base["entry_px"]), base["touch_size"]),
    }
    out = {}
    for name, shares in plans.items():
        pnl = per_share * shares.to_numpy()
        daily = pd.Series(pnl).groupby(base["day"].to_numpy()).sum()
        top = pd.Series(pnl).groupby(base["asset"].to_numpy()).sum()
        out[name] = {
            "total_usd": round(float(pnl.sum()), 2),
            "daily_mean": round(float(daily.mean()), 2),
            "daily_sd": round(float(daily.std(ddof=1)), 2),
            "by_asset_usd": {a: round(float(v), 2) for a, v in top.items()},
        }
    return out


EPOCH4_NET_CENTS = -31.0  # the live shadow over epoch 4's first hour


def _cluster_se(df: pd.DataFrame) -> float | None:
    """S.e. of the mean net with entries clustered by window start (all assets)."""
    c = df["open_unix"].nunique()
    if c < 2:
        return None
    sums = (df["net"] - df["net"].mean()).groupby(df["open_unix"]).sum()
    return float(math.sqrt(c / (c - 1) * float((sums**2).sum())) / len(df))


def regime(entries: pd.DataFrame, days: int) -> dict[str, Any]:
    base = entries[entries["variant"] == "base"].copy()
    if base.empty:
        return {}
    base["hour"] = base["open_unix"] // 3600
    base["hour_utc"] = base["hour"] % 24
    hours = base.groupby("hour").agg(n=("net", "size"), wins=("win", "sum"), net=("net", "mean"))
    busy = hours[hours["n"] >= 10]
    bad_hours = {
        "hours_with_10_plus": int(len(busy)),
        "share_zero_wins": None if busy.empty else round(float((busy["wins"] == 0).mean()), 4),
        "share_net_at_or_below_epoch4": None
        if busy.empty
        else round(float((busy["net"] <= EPOCH4_NET_CENTS).mean()), 4),
        "hour_net_quantiles": {
            f"p{q}": round(float(busy["net"].quantile(q / 100)), 1) for q in (1, 5, 10, 50)
        }
        if not busy.empty
        else {},
    }
    by_hour = {
        int(h): {
            "n": int(len(g)),
            "win_rate": round(float(g["win"].mean()), 3),
            "net_cents": round(float(g["net"].mean()), 2),
            "k_spread_median": round(float((g["k_ask"] - g["k_bid"]).median()), 3),
        }
        for h, g in base.groupby("hour_utc")
    }
    night = base["hour_utc"].isin(US_NIGHT_UTC)
    k_ext = (((base["k_bid"] + base["k_ask"]) / 2 - 0.5).abs()
             > ((base["y_bid"] + base["y_ask"]) / 2 - 0.5).abs())  # fmt: skip
    first = base.loc[base.assign(a=base["gap"].abs()).groupby("open_unix")["a"].idxmax()]
    per_window = base.groupby("open_unix").size()
    return {
        "bad_hours": bad_hours,
        "by_hour_utc": by_hour,
        "us_night": _stats(base[night], days),
        "us_day": _stats(base[~night], days),
        "kalshi_extreme": {
            "kalshi_more_extreme": _stats(base[k_ext], days),
            "kalshi_less_extreme": _stats(base[~k_ext], days),
            "floor_0_15": _stats(base[base["entry_px"] >= 0.15], days),
            "kext_and_floor": _stats(base[k_ext & (base["entry_px"] >= 0.15)], days),
        },
        "clustering": {
            "windows": int(len(per_window)),
            "entries_per_window_mean": round(float(per_window.mean()), 2),
            "share_windows_3_plus": round(float((per_window >= 3).mean()), 4),
            "se_entry": _stats(base, days).get("se"),
            "se_window_clustered": None if (se := _cluster_se(base)) is None else round(se, 2),
            "one_per_window_max_gap": _stats(first, days),
        },
    }


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix_sweeps")
    parser.add_argument("--split", choices=("discovery", "holdout", "earlier"), required=True)
    parser.add_argument("--data", type=Path, default=Path("research/data/helix"))
    parser.add_argument(
        "--inventory", type=Path, default=Path("research/data/helix_inventory.json")
    )
    parser.add_argument(
        "--resolutions", type=Path, default=Path("research/data/resolutions.parquet")
    )
    parser.add_argument("--skip-slices", action="store_true", help="no latency pass (faster)")
    args = parser.parse_args()
    if args.split == "holdout" and not PREREG.is_file():
        raise SystemExit("research/PREREG.md is missing: write the holdout comparisons first")
    inv = json.loads(args.inventory.read_text())["days"]
    role = {"discovery": "discovery", "holdout": "holdout", "earlier": "earlier_regime"}[args.split]
    days = sorted(d for d, v in inv.items() if v["role"] == role and v["usable"])
    res = pd.read_parquet(args.resolutions)
    frames = load(args.data, days)
    cross = settle(frames["crossings"], res)
    entries = cross[cross["enter"]]
    result: dict[str, Any] = {
        "split": args.split,
        "days": days,
        "excluded_days": sorted(d for d, v in inv.items() if v["role"] == role and not v["usable"]),
        "variants": variants(cross, len(days)),
        "thresholds": thresholds(frames["windows"]),
        "spreads": spreads(entries),
        "sizing": sizing(entries),
        "regime": regime(entries, len(days)),
        "nulls": nulls(args.data, days, cross, res),
    }
    if not args.skip_slices:
        result["latency"] = latency(args.data, days, entries)
    out = args.data.parent / f"helix_sweeps_{args.split}.json"
    out.write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: result[k] for k in ("split", "variants")}, indent=1, default=str))


if __name__ == "__main__":
    _main()
