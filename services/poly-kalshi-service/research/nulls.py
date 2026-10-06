"""The null battery (manual §5), on our own tape.

    python -m research.nulls --run research/data/run1

Reads a ``research.replay run`` output (``grid.parquet`` and ``journal/``) and
books, at the recorded touch with the real fee curve, held to Polymarket's
own resolution:

| arm | when | leg |
|---|---|---|
| ``signal`` | the engine's first crossing, every one (cap ignored) | the gap's cheap leg |
| ``signal_capped`` | the same, only entries at or below the cap | same |
| ``random_same_rule`` | a random second in the same window | the leg the gap's sign points to |
| ``random_cheap`` | a random second | the cheaper of YES / NO |
| ``fixed_cheap`` | second 300 | the cheaper of YES / NO |

The reproduce-or-stop check from the manual: ``signal`` edge about +5 pp and
``random_same_rule`` about 0 pp. If the signal is real and the nulls are not
flat, suspect window identity or settlement mapping before anything else.

Edge (pp) = win rate - average price. Net ¢/share subtracts the fee. Band
excess = the signal arm's mean (payout - price) minus the matched
``random_same_rule`` null's, in the same entry-price band, in cents.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from poly_kalshi_service.fees import taker_fee
from poly_kalshi_service.gates import BANDS, band_of
from poly_kalshi_service.trades import read_journal

__all__ = ["arms", "summarise"]

FIXED_SEC = 300


def _bookings_from_grid(
    grid: pd.DataFrame, rng: np.random.Generator, early: int, late: int
) -> dict[str, pd.DataFrame]:
    live = grid[(grid["sec"] >= early) & (grid["sec"] <= 900 - late)].copy()
    live["k_mid"] = (live["k_bid"] + live["k_ask"]) / 2
    live["y_mid"] = (live["y_bid"] + live["y_ask"]) / 2
    live["gap"] = live["k_mid"] - live["y_mid"]
    groups = live.groupby(["asset", "open_unix"], sort=False)
    picks = groups.sample(n=1, random_state=rng)
    fixed = live[live["sec"] == FIXED_SEC]

    def same_rule(df: pd.DataFrame) -> pd.DataFrame:
        side = np.where(df["gap"] > 0, "yes", "no")
        price = np.where(df["gap"] > 0, df["y_ask"], df["n_ask"])
        return pd.DataFrame(
            {"asset": df["asset"], "open_unix": df["open_unix"], "side": side, "px": price}
        )

    def cheap(df: pd.DataFrame) -> pd.DataFrame:
        yes_cheaper = df["y_ask"] <= df["n_ask"]
        side = np.where(yes_cheaper, "yes", "no")
        price = np.where(yes_cheaper, df["y_ask"], df["n_ask"])
        return pd.DataFrame(
            {"asset": df["asset"], "open_unix": df["open_unix"], "side": side, "px": price}
        )

    return {
        "random_same_rule": same_rule(picks),
        "random_cheap": cheap(picks),
        "fixed_cheap": cheap(fixed),
    }


def arms(
    grid: pd.DataFrame,
    journal: list[dict[str, Any]],
    *,
    cap: float,
    early: int = 20,
    late: int = 60,
    seed: int = 7,
) -> dict[str, pd.DataFrame]:
    outcomes = {
        (r["asset"], r["open_unix"]): r["outcome"]
        for r in journal
        if r.get("type") == "resolution" and r.get("venue") == "poly"
    }
    decisions = pd.DataFrame(
        [
            {
                "asset": r["asset"],
                "open_unix": r["open_unix"],
                "side": r["side"],
                "px": r["entry_px"],
            }
            for r in journal
            if r.get("type") == "decision" and r.get("entry_px") is not None
        ],
        columns=["asset", "open_unix", "side", "px"],
    )
    books = {"signal": decisions, "signal_capped": decisions[decisions["px"] <= cap]}
    books |= _bookings_from_grid(grid, np.random.default_rng(seed), early, late)
    out = {}
    for name, df in books.items():
        df = df.copy()
        df["outcome"] = [
            outcomes.get((a, o)) for a, o in zip(df["asset"], df["open_unix"], strict=True)
        ]
        df = df[df["outcome"].notna() & (df["px"] > 0) & (df["px"] < 1)]
        df["win"] = (df["side"] == df["outcome"]).astype(float)
        df["fee"] = [taker_fee(p, 1.0) for p in df["px"]]
        df["net_cents"] = 100 * (df["win"] - df["px"] - df["fee"])
        df["gross_cents"] = 100 * (df["win"] - df["px"])
        df["day"] = pd.to_datetime(df["open_unix"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
        df["band"] = [band_of(p) for p in df["px"]]
        out[name] = df
    return out


def _arm_row(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0}
    daily = df.groupby("day")["gross_cents"].mean()
    daily_net = df.groupby("day")["net_cents"].mean()
    t = None
    if len(daily_net) > 1 and daily_net.std(ddof=1) > 0:
        t = float(daily_net.mean() / (daily_net.std(ddof=1) / math.sqrt(len(daily_net))))
    return {
        "n": int(len(df)),
        "avg_px": round(float(df["px"].mean()), 4),
        "win_rate": round(float(df["win"].mean()), 4),
        "edge_pp": round(100 * float(df["win"].mean() - df["px"].mean()), 2),
        "net_cents": round(float(df["net_cents"].mean()), 2),
        "days_positive": f"{int((daily > 0).sum())}/{len(daily)}",
        "day_t_net": None if t is None else round(t, 2),
    }


def summarise(books: dict[str, pd.DataFrame]) -> dict[str, Any]:
    signal, null = books["signal"], books["random_same_rule"]
    bands = {}
    for lo, _hi in BANDS:
        name = band_of(lo)
        s, n = signal[signal["band"] == name], null[null["band"] == name]
        excess = None
        if len(s) and len(n):
            excess = round(float(s["gross_cents"].mean() - n["gross_cents"].mean()), 2)
        bands[name] = {"n_signal": int(len(s)), "n_null": int(len(n)), "excess_cents": excess}
    per_asset = {asset: _arm_row(df) for asset, df in signal.groupby("asset")}
    return {
        "arms": {name: _arm_row(df) for name, df in books.items()},
        "bands_signal_vs_random_same_rule": bands,
        "signal_by_asset": per_asset,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.nulls")
    parser.add_argument("--run", type=Path, required=True, help="a research.replay run directory")
    parser.add_argument("--cap", type=float, default=0.60)
    parser.add_argument("--early", type=int, default=20)
    parser.add_argument("--late", type=int, default=60)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    grid = pd.read_parquet(args.run / "grid.parquet")
    journal = list(read_journal(args.run / "journal"))
    books = arms(grid, journal, cap=args.cap, early=args.early, late=args.late, seed=args.seed)
    result = summarise(books)
    (args.run / "nulls.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _main()
