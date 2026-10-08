"""PLAN 4.3 on the Helix tape: a Chainlink fair-value signal against the Kalshi gap.

    python -m research.helix_fairvalue --split discovery

Reads the per-second grids of ``research.helix_replay`` (each leg's touch and
Chainlink's price ``s`` at the end of every second) and the venue resolutions.
Chainlink is on the tape from 2026-08-11, so only days from then are used.

1. **Which Chainlink number settles the market.** Since 2026-08-07 Polymarket
   resolves on Chainlink's 60 s TWAP stream. Discovery: the 60 s TWAP at the close
   against the 60 s TWAP at the open reproduces 99.05% of 33,235 windows; the
   tape's strike (spot at the open) 95.8%, the last tick 94.4%. So K is the TWAP
   at the open (``k_twap``), not the strike.
2. **Fair value.** ``P(up) = Φ((ln E[X] − ln K) / sd)`` where X is the target
   (for a 60 s average: a partly observed average of a driftless log-normal
   walk), K the TWAP at the open, sd from σ, a trailing per-second realised volatility of
   Chainlink over ``W`` seconds, and the time left.
3. **The variant**, evaluated per second (one decision per window, first
   second it fires, inside the entry window, at or below the cap): buy YES when
   ``P(up) − ask − fee > edge``, NO when ``(1 − P(up)) − ask − fee > edge``.
   ``W`` and ``edge`` are swept; on the same windows and the same per-second
   grid, the Kalshi gap at 0.10 is scored the same way, so the two signals are
   compared like for like (the tick-level gap is in ``helix_sweeps``).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

WINDOWS_W = (120, 300, 900)
EDGES = (0.02, 0.04, 0.06, 0.08, 0.10)
EARLY, LATE, CAP = 20, 60, 0.60
AVG_S = 60


GRID_COLUMNS = ["asset", "open_unix", "sec", "k_bid", "k_ask", "y_bid", "y_ask", "n_bid",
                "n_ask", "s"]  # fmt: skip


def load(data: Path, days: list[str], asset: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One asset's per-second grid and windows over ``days`` (one asset at a time: the
    whole discovery grid does not fit in memory with its derived columns)."""
    grids, wins = [], []
    for day in days:
        d = data / day
        if not (d / "summary.json").is_file():
            continue
        g = pd.read_parquet(d / "grid.parquet", columns=GRID_COLUMNS,
                            filters=[("asset", "==", asset)])  # fmt: skip
        g = g[g["s"].notna()]
        g["day"] = day
        grids.append(g)
        w = pd.read_parquet(d / "windows.parquet", filters=[("asset", "==", asset)])
        w["day"] = day
        wins.append(w)
    return pd.concat(grids, ignore_index=True), pd.concat(wins, ignore_index=True)


def _fee(px: pd.Series) -> pd.Series:
    return 0.07 * px * (1 - px)  # the schedule on every Helix window (resolutions.parquet)


def with_reference(wins: pd.DataFrame) -> pd.DataFrame:
    """Each window with ``k_twap``: the 60 s Chainlink TWAP as it stood at the open,
    i.e. the previous window's last-60 s mean. This, not the tape's strike (spot at
    the open), is what Polymarket compares the closing TWAP with (``settlement_rules``)."""
    prev = wins[["asset", "open_unix", "cl_twap60", "cl_close_last"]].rename(
        columns={"cl_twap60": "k_twap", "cl_close_last": "last_prev"}
    )
    prev = prev.assign(open_unix=prev["open_unix"] + 900)
    return wins.merge(prev, how="left", on=["asset", "open_unix"])


def settlement_rules(wins: pd.DataFrame, res: pd.DataFrame) -> dict[str, Any]:
    """Which Chainlink comparison reproduces Polymarket's outcome, over every window
    with Chainlink on both sides (from the raw stream, not the grid)."""
    w = wins.drop(columns=["poly_outcome"], errors="ignore").merge(
        res[["asset", "open_unix", "poly_outcome"]]
    )
    w = w[w["poly_outcome"].notna() & w["cl_twap60"].notna() & w["k_twap"].notna()]
    up = w["poly_outcome"] == "yes"
    rules = {
        "last_tick >= strike (tape)": w["cl_close_last"] >= w["strike"],
        "twap60 >= strike (tape)": w["cl_twap60"] >= w["strike"],
        "last_tick >= last_tick at open": w["cl_close_last"] >= w["last_prev"],
        "twap60 >= twap60 at open": w["cl_twap60"] >= w["k_twap"],
    }
    return {"windows": int(len(w))} | {
        k: round(float((p == up).mean()), 4) for k, p in rules.items()
    }


def ndtr(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF via erf (Abramowitz and Stegun 7.1.26, |error| < 1.5e-7)."""
    z = np.abs(x) / math.sqrt(2)
    t = 1 / (1 + 0.3275911 * z)
    poly = t * (
        0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429)))
    )
    erf = 1 - poly * np.exp(-z * z)
    return 0.5 * (1 + np.sign(x) * erf)


def _sigma(grid: pd.DataFrame, w: int) -> pd.Series:
    """Trailing per-second log-return s.d. of Chainlink over ``w`` seconds, per asset."""
    g = grid.sort_values(["asset", "open_unix", "sec"])
    t = g["open_unix"] + g["sec"].astype(np.int64)
    r = np.log(g["s"]).groupby(g["asset"]).diff()
    gap = t.groupby(g["asset"]).diff()
    r = r.where(gap == 1)  # a missing second is not a 1 s return
    sd = r.groupby(g["asset"]).transform(lambda x: x.rolling(w, min_periods=w // 3).std())
    return sd.reindex(grid.index)


def p_up(
    s: np.ndarray, k: np.ndarray, sigma: np.ndarray, sec: np.ndarray, avg_sum: np.ndarray
) -> np.ndarray:
    """P(average of the last 60 s >= K) with ``sec`` seconds elapsed, driftless log walk.

    Before the averaging period, X ≈ S·(1 + noise) with variance σ²((τ − 60) + 60/3);
    inside it, the observed part is known (``avg_sum``) and the rest averages
    the remaining path: var = σ² · τ_rem³ / (3 · 60²) in log terms of S.
    """
    tau = 900.0 - sec
    in_avg = tau < AVG_S
    var = np.where(
        in_avg, sigma**2 * tau**3 / (3 * AVG_S**2), sigma**2 * ((tau - AVG_S) + AVG_S / 3)
    )
    mean = np.where(in_avg, (avg_sum + s * tau) / AVG_S, s)
    sd = np.sqrt(np.maximum(var, 1e-12))
    return ndtr((np.log(mean) - np.log(k)) / sd)


def scan(df: pd.DataFrame, signal: str, edge: float) -> pd.DataFrame:
    """First second per window the signal fires: one decision, as the live rule."""
    df = df[(df["sec"] >= EARLY) & (df["sec"] <= 900 - LATE)]
    fee_y, fee_n = _fee(df["y_ask"]), _fee(df["n_ask"])
    if signal == "fair":
        buy_yes = df["p_up"] - df["y_ask"] - fee_y > edge
        buy_no = (1 - df["p_up"]) - df["n_ask"] - fee_n > edge
    else:  # the Kalshi gap at ``edge`` as threshold
        gap = (df["k_bid"] + df["k_ask"]) / 2 - (df["y_bid"] + df["y_ask"]) / 2
        buy_yes, buy_no = gap >= edge - 1e-9, gap <= -edge + 1e-9
    fired = df[buy_yes | buy_no].copy()
    fired["side"] = np.where(buy_yes[fired.index], "yes", "no")
    first = fired.sort_values("sec").groupby(["asset", "open_unix"]).head(1)
    first = first.assign(px=np.where(first["side"] == "yes", first["y_ask"], first["n_ask"]))
    return first[first["px"] <= CAP]  # above the cap: the window is consumed, no entry


def _brier(p: pd.Series, up: pd.Series) -> float:
    return round(float(((p - up) ** 2).mean()), 4)


def score(entries: pd.DataFrame, res: pd.DataFrame) -> dict[str, Any]:
    e = entries.merge(res[["asset", "open_unix", "poly_outcome"]])
    e = e[e["poly_outcome"].notna()]
    if e.empty:
        return {"n": 0}
    win = (e["side"] == e["poly_outcome"]).astype(float)
    net = 100 * (win - e["px"] - _fee(e["px"]))
    daily = net.groupby(e["day"]).mean()
    t = None
    if len(daily) > 1 and daily.std(ddof=1) > 0:
        t = float(daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))))
    return {
        "n": int(len(e)),
        "per_day": round(len(e) / max(e["day"].nunique(), 1), 1),
        "win_rate": round(float(win.mean()), 4),
        "avg_px": round(float(e["px"].mean()), 4),
        "net_cents": round(float(net.mean()), 2),
        "se": round(float(net.std(ddof=1) / math.sqrt(len(net))), 2) if len(net) > 1 else None,
        "day_t": None if t is None else round(t, 2),
        "median_sec": round(float(e["sec"].median()), 0),
        "by_asset": {
            a: {"n": int(len(n)), "net_cents": round(float(n.mean()), 2)}
            for a, n in net.groupby(e["asset"])
        },
    }  # fmt: skip


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix_fairvalue")
    parser.add_argument("--split", choices=("discovery", "holdout"), required=True)
    parser.add_argument("--data", type=Path, default=Path("research/data/helix"))
    parser.add_argument(
        "--inventory", type=Path, default=Path("research/data/helix_inventory.json")
    )
    parser.add_argument(
        "--resolutions", type=Path, default=Path("research/data/resolutions.parquet")
    )
    args = parser.parse_args()
    inv = json.loads(args.inventory.read_text())["days"]
    days = sorted(
        d for d, v in inv.items() if v["role"] == args.split and v["usable"] and v["chainlink"]
    )
    res = pd.read_parquet(args.resolutions)
    result: dict[str, Any] = {"split": args.split, "days": days}
    feats, cal, fired = [], {w: [] for w in WINDOWS_W}, {}
    for asset in sorted({a for a in res["asset"].unique()}):
        grid, wins = load(args.data, days, asset)
        if grid.empty:
            continue
        wins = with_reference(wins)
        feats.append(wins)
        grid = grid.merge(wins[["asset", "open_unix", "k_twap"]], how="left")
        grid = grid[grid["k_twap"].notna() & (grid["k_twap"] > 0)]
        grid = grid.sort_values(["open_unix", "sec"]).reset_index(drop=True)
        in_avg = grid["sec"] >= 900 - AVG_S
        grid["avg_sum"] = grid["s"].where(in_avg, 0.0).groupby(grid["open_unix"]).cumsum()
        for w in WINDOWS_W:
            sigma = _sigma(grid, w)
            ok = sigma.notna() & (sigma > 0)
            df = grid[ok].copy()
            df["p_up"] = p_up(
                df["s"].to_numpy(), df["k_twap"].to_numpy(), sigma[ok].to_numpy(),
                df["sec"].to_numpy(dtype=float), df["avg_sum"].to_numpy(),
            )  # fmt: skip
            cal[w].append(df[df["sec"] % 60 == 0])  # calibration sample: one row a minute
            for e in EDGES:
                fired.setdefault(("fair", w, e), []).append(scan(df, "fair", e))
            if w == WINDOWS_W[1]:
                for t in (0.08, 0.10, 0.12, 0.15):
                    fired.setdefault(("gap", w, t), []).append(scan(df, "gap", t))
        del grid
        print(asset, "done", flush=True)
    result["settlement"] = settlement_rules(pd.concat(feats, ignore_index=True), res)
    fair: dict[str, Any] = {}
    for w in WINDOWS_W:
        c = pd.concat(cal[w], ignore_index=True).merge(res[["asset", "open_unix", "poly_outcome"]])
        c = c[c["poly_outcome"].notna()]
        c["up"] = (c["poly_outcome"] == "yes").astype(float)
        bins = pd.cut(c["p_up"], np.linspace(0, 1, 11), include_lowest=True)
        fair[f"W{w}"] = {
            "calibration": {
                str(b): {"n": int(len(x)), "up_rate": round(float(x["up"].mean()), 3)}
                for b, x in c.groupby(bins, observed=True)
            },
            "brier": _brier(c["p_up"], c["up"]),
            "brier_poly_mid": _brier((c["y_bid"] + c["y_ask"]) / 2, c["up"]),
            "brier_kalshi_mid": _brier((c["k_bid"] + c["k_ask"]) / 2, c["up"]),
            "edges": {
                f"{e:.2f}": score(pd.concat(fired[("fair", w, e)], ignore_index=True), res)
                for e in EDGES
            },
        }
    result["kalshi_gap_per_second"] = {
        f"{t:.2f}": score(pd.concat(fired[("gap", WINDOWS_W[1], t)], ignore_index=True), res)
        for t in (0.08, 0.10, 0.12, 0.15)
    }
    result["fair_value"] = fair
    out = args.data.parent / f"helix_fairvalue_{args.split}.json"
    out.write_text(json.dumps(result, indent=1, default=str))
    keys = ("settlement", "kalshi_gap_per_second")
    print(json.dumps({k: result.get(k) for k in keys}, indent=1))


if __name__ == "__main__":
    _main()
