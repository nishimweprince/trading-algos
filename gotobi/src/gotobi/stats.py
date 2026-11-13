"""Trade statistics in the paper's terms plus the significance tests in PREREG.md."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def summarize(gross_pips: pd.Series, cost_pips: float, legs: pd.Series | None = None) -> dict:
    """N, mean, t, PF, PR, W, total for net pips. ``legs`` = round trips per row (default 1)."""
    g = gross_pips.dropna()
    n_legs = legs.reindex(g.index).fillna(1) if legs is not None else pd.Series(1, index=g.index)
    x = (g - cost_pips * n_legs).to_numpy(dtype=float)
    n = len(x)
    if n == 0:
        return {"N": 0}
    mean = x.mean()
    sd = x.std(ddof=1) if n > 1 else float("nan")
    t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    wins, losses = x[x > 0], x[x < 0]
    return {
        "N": n,
        "mean": mean,
        "sd": sd,
        "t": t,
        "p_one_sided": _norm_sf(t) if not math.isnan(t) else float("nan"),
        "PF": wins.sum() / -losses.sum() if len(losses) else float("inf"),
        "PR": (wins.mean() / -losses.mean()) if len(wins) and len(losses) else float("nan"),
        "W": len(wins) / n,
        "total": x.sum(),
    }


def welch(a: pd.Series, b: pd.Series) -> dict:
    """Welch t for mean(a) - mean(b); costs cancel when both sides pay the same."""
    a, b = a.dropna().to_numpy(float), b.dropna().to_numpy(float)
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    diff = a.mean() - b.mean()
    t = diff / math.sqrt(va + vb)
    return {"diff": diff, "t": t, "p_one_sided": _norm_sf(t), "Na": len(a), "Nb": len(b)}


def holm(pvals: dict[str, float], alpha: float = 0.05) -> dict[str, bool]:
    """Holm step-down: name -> rejected at family-wise ``alpha``."""
    order = sorted(pvals, key=lambda k: pvals[k])
    m = len(order)
    out, still = {}, True
    for i, k in enumerate(order):
        still = still and pvals[k] <= alpha / (m - i)
        out[k] = still
    return out


def by_year(gross_pips: pd.Series, cost_pips: float) -> pd.DataFrame:
    g = gross_pips.dropna()
    years = pd.Index([d.year for d in g.index], name="year")
    rows = {y: summarize(g[years == y], cost_pips) for y in sorted(set(years))}
    return pd.DataFrame(rows).T[["N", "mean", "t", "PF", "W", "total"]]


def bootstrap_mean_ci(x: pd.Series, n_boot: int = 10_000, seed: int = 7,
                      block: int = 20) -> tuple[float, float]:
    """Moving-block bootstrap 95% CI of the mean (blocks of consecutive trades)."""
    v = x.dropna().to_numpy(float)
    n = len(v)
    if n < block * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = v[idx].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))
