"""Headline statistics for a run (and for each walk-forward fold in M4)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

__all__ = ["summary"]

DAY_MS = 86_400_000


def profit_factor(net: pd.Series) -> float:
    wins = net[net > 0].sum()
    losses = -net[net < 0].sum()
    if losses == 0:
        return math.inf if wins > 0 else math.nan
    return float(wins / losses)


def daily_equity(equity: pd.DataFrame) -> pd.Series:
    """Last equity of each UTC day, from the hourly curve."""
    day = (equity["time"] + 1) // DAY_MS
    return equity.groupby(day)["equity"].last()


def sharpe_daily(equity: pd.DataFrame) -> float:
    rets = daily_equity(equity).pct_change().dropna()
    if len(rets) < 2 or rets.std() == 0:
        return math.nan
    return float(rets.mean() / rets.std() * math.sqrt(365))


def max_drawdown(equity: pd.DataFrame) -> float:
    eq = equity["equity"].to_numpy(float)
    if len(eq) == 0:
        return math.nan
    peak = np.maximum.accumulate(eq)
    return float((1 - eq / peak).max())


def cagr(equity: pd.DataFrame) -> float:
    if len(equity) < 2:
        return math.nan
    years = (equity["time"].iloc[-1] - equity["time"].iloc[0]) / (365.25 * DAY_MS)
    start, end = equity["equity"].iloc[0], equity["equity"].iloc[-1]
    if years <= 0 or start <= 0 or end <= 0:
        return math.nan
    return float((end / start) ** (1 / years) - 1)


def summary(trades: pd.DataFrame, equity: pd.DataFrame) -> dict:
    n = len(trades)
    net = trades["net"] if n else pd.Series(dtype=float)
    out = {
        "trades": n,
        "win_rate": float((net > 0).mean()) if n else math.nan,
        "profit_factor": profit_factor(net) if n else math.nan,
        "expectancy_r": float(trades["r_mult"].mean()) if n else math.nan,
        "net_pnl": float(net.sum()) if n else 0.0,
        "fees": float(trades["fees"].sum()) if n else 0.0,
        "funding": float(trades["funding"].sum()) if n else 0.0,
        "start_equity": float(equity["equity"].iloc[0]) if len(equity) else math.nan,
        "end_equity": float(equity["equity"].iloc[-1]) if len(equity) else math.nan,
        "cagr": cagr(equity),
        "max_drawdown": max_drawdown(equity),
        "sharpe_daily": sharpe_daily(equity),
    }
    if n:
        per = trades.groupby("symbol").agg(
            trades=("net", "size"), net=("net", "sum"), expectancy_r=("r_mult", "mean")
        )
        out["per_symbol"] = per.reset_index().to_dict("records")
        out["exit_reasons"] = trades["exit_reason"].value_counts().to_dict()
    return out
