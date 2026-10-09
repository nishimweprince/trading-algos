"""Block-bootstrap Monte Carlo over out-of-sample trades.

Each trade becomes a return on the equity it was taken with (net PnL divided
by equity at entry, read from the stitched OOS curve). A path draws blocks of
``block`` consecutive trades with replacement until it has as many trades as
the sample, then compounds them. Over ``n_paths`` paths (seeded, so reruns
match) it reports the 5th/50th/95th percentiles of CAGR (over the sample's
calendar span), maximum drawdown and the longest recovery (in days, from
the average spacing between trades), plus the probability of a -50% path.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

__all__ = ["monte_carlo", "trade_returns"]

DAY_MS = 86_400_000


def trade_returns(trades: pd.DataFrame, equity: pd.DataFrame) -> np.ndarray:
    if trades.empty:
        return np.array([])
    t = trades.sort_values("entry_time")
    eq = equity.sort_values("time")
    idx = np.searchsorted(eq["time"].to_numpy(), t["entry_time"].to_numpy(), side="right") - 1
    base = eq["equity"].to_numpy()[np.clip(idx, 0, len(eq) - 1)]
    return (t["net"].to_numpy() / base).astype(float)


def _paths(r: np.ndarray, n_paths: int, block: int, rng: np.random.Generator) -> np.ndarray:
    n = len(r)
    block = min(block, n)
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_paths, -1)[:, :n]
    return r[idx]


def _stats(rets: np.ndarray, years: float, days_per_trade: float) -> dict[str, np.ndarray]:
    curve = np.cumprod(1 + rets, axis=1)
    curve = np.concatenate([np.ones((len(curve), 1)), curve], axis=1)
    peak = np.maximum.accumulate(curve, axis=1)
    dd = 1 - curve / peak
    under = dd > 0
    # longest run of consecutive trades spent below a previous peak
    longest = np.zeros(len(curve))
    run = np.zeros(len(curve))
    for k in range(curve.shape[1]):
        run = np.where(under[:, k], run + 1, 0)
        longest = np.maximum(longest, run)
    final = curve[:, -1]
    return {
        "cagr": np.where(final > 0, final ** (1 / years) - 1, -1.0),
        "max_drawdown": dd.max(axis=1),
        "recovery_days": longest * days_per_trade,
        "min_equity": curve.min(axis=1),
    }


def monte_carlo(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    n_paths: int = 10_000,
    block: int = 5,
    seed: int = 0,
) -> dict:
    r = trade_returns(trades, equity)
    if len(r) < 2:
        return {"trades": len(r), "note": "too few trades"}
    span = (equity["time"].iloc[-1] - equity["time"].iloc[0]) / DAY_MS
    years = max(span / 365.25, 1e-9)
    stats = _stats(_paths(r, n_paths, block, np.random.default_rng(seed)), years, span / len(r))
    pct = {
        k: {p: float(np.percentile(v, p)) for p in (5, 50, 95)}
        for k, v in stats.items()
        if k != "min_equity"
    }
    return {
        "trades": len(r),
        "paths": n_paths,
        "block": block,
        "years": years,
        **pct,
        "p_minus_50": float((stats["min_equity"] <= 0.5).mean()),
    }
