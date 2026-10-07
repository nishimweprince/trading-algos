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
    return out


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
        "nulls": nulls(args.data, days, cross, res),
    }
    if not args.skip_slices:
        result["latency"] = latency(args.data, days, entries)
    out = args.data.parent / f"helix_sweeps_{args.split}.json"
    out.write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: result[k] for k in ("split", "variants")}, indent=1, default=str))


if __name__ == "__main__":
    _main()
