"""Run-up distributions: U_T(t) = max_{t < s <= t+T} high_s / close_t - 1."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def runup(high: pd.Series, close: pd.Series, T: int) -> pd.Series:
    """Forward run-up over the next ``T`` bars, excluding bar t itself.

    Windows that would run past the end of the series are NaN, not truncated.
    Internal missing bars are skipped (the max over observed bars is used).
    """
    fwd_max = high.rolling(T, min_periods=1).max().shift(-T)
    u = fwd_max / close - 1.0
    u.iloc[max(0, len(u) - T) :] = float("nan")  # windows needing data past the end
    return u.rename(f"U_{T}")


def runups(spot: pd.DataFrame, windows: Iterable[int]) -> pd.DataFrame:
    return pd.concat([runup(spot["high"], spot["close"], T) for T in windows], axis=1)


def runup_table(U: pd.DataFrame, alphas: Iterable[float]) -> pd.DataFrame:
    rows = []
    alphas = list(alphas)
    for col in U.columns:
        s = U[col].dropna()
        row: dict[str, float | int | str] = {"window_h": int(col.split("_")[1]), "n": len(s)}
        for a in alphas:
            row[f"q{a:g}"] = float(s.quantile(a)) if len(s) else float("nan")
        row["max"] = float(s.max()) if len(s) else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


def q_alpha(table: pd.DataFrame, T: int, alpha: float) -> float:
    row = table[table["window_h"] == T]
    if row.empty:
        raise KeyError(f"run-up window {T}h not computed; add it to windows.runup_windows_h")
    return float(row[f"q{alpha:g}"].iloc[0])
