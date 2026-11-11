"""Variant P: staked long + USD-margined perp short.

g_t = y_t + 8760 * f_h,t - c_P
R_W = mean_W(g_t + beta_P * r_res,t) / k_P
X_W = R_W - mean_W(r_f,t)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..config import Config
from ..normalize.funding import HOURS_PER_YEAR
from .reserve import beta_p, k_p
from .stats import describe, longest_underwater_days, share_negative

PERP_VARIANTS: dict[str, tuple[str, str]] = {
    # id: (long, funding source)
    "P-LST-VEL": ("lst", "velocity"),
    "P-LST-BIN": ("lst", "binance"),
    "P-NAT-VEL": ("native", "velocity"),
    "P-NAT-BIN": ("native", "binance"),
}
MIN_WINDOW_COVERAGE = 0.9


def perp_cost(cfg: Config) -> float:
    c = cfg.costs
    return (c.perp_round_trip_bps / c.hold_years_assumed + c.resize_bps_per_year) / 1e4


@dataclass
class PerpResult:
    variant: str
    long: str
    source: str
    T_h: int
    q_runup: float
    beta: float
    k: float
    cost: float
    hourly: pd.DataFrame  # g, R_h, x_h, components
    windows: pd.DataFrame  # long: window_days, end, R_W, X_W, sol_ret_30d
    summary: dict[str, Any] = field(default_factory=dict)
    regime: pd.DataFrame = field(default_factory=pd.DataFrame)


def hourly_carry(
    f_h: pd.Series,
    y: pd.Series,
    r_f: pd.Series,
    r_res: pd.Series | float,
    beta: float,
    k: float,
    cost: float,
) -> pd.DataFrame:
    df = pd.DataFrame({"f_h": f_h, "y": y, "r_f": r_f})
    if isinstance(r_res, pd.Series):
        df["r_res"] = r_res.reindex(df.index).fillna(0.0)
    else:
        df["r_res"] = float(r_res)
    df = df.dropna(subset=["f_h", "y", "r_f"])
    df["funding_ann"] = df["f_h"] * HOURS_PER_YEAR
    df["g"] = df["y"] + df["funding_ann"] - cost
    df["R_h"] = (df["g"] + beta * df["r_res"]) / k
    df["x_h"] = df["R_h"] - df["r_f"]
    return df


def rolling_windows(hourly: pd.DataFrame, days: list[int]) -> pd.DataFrame:
    """Rolling windows of ``days`` days, stepping one day, on hourly R_h/x_h."""
    if hourly.empty:
        return pd.DataFrame(columns=["window_days", "end", "R_W", "X_W", "hours"])
    d = hourly[["R_h", "x_h"]].resample("D").agg(["sum", "count"])
    d.columns = ["R_sum", "R_n", "x_sum", "x_n"]
    full = pd.date_range(d.index.min(), d.index.max(), freq="D", tz="UTC")
    d = d.reindex(full).fillna(0.0)
    frames = []
    for D in days:
        rs = d.rolling(D, min_periods=D).sum()
        ok = rs["R_n"] >= MIN_WINDOW_COVERAGE * 24 * D
        w = pd.DataFrame(
            {
                "window_days": D,
                "end": pd.DatetimeIndex(rs.index) + pd.Timedelta(days=1),
                "R_W": rs["R_sum"] / rs["R_n"],
                "X_W": rs["x_sum"] / rs["x_n"],
                "hours": rs["R_n"],
            }
        )[ok.to_numpy()]
        frames.append(w)
    return pd.concat(frames, ignore_index=True)


def trailing_return(close: pd.Series, days: int, at: pd.Series) -> pd.Series:
    c = close.dropna()
    if c.empty:
        return pd.Series(np.nan, index=at.index)
    a = pd.DatetimeIndex(at) - pd.Timedelta(hours=1)  # last bar inside the window
    now = c.reindex(a, method="ffill").to_numpy()
    then = c.reindex(a - pd.Timedelta(days=days), method="ffill").to_numpy()
    return pd.Series(now / then - 1.0, index=at.index)


def regime_split(windows: pd.DataFrame, D: int = 30) -> pd.DataFrame:
    w = windows[windows["window_days"] == D].dropna(subset=["sol_ret"])
    if len(w) < 3:
        return pd.DataFrame(columns=["tercile", "ret_lo", "ret_hi", "n", "X_median", "X_mean"])
    w = w.assign(tercile=pd.qcut(w["sol_ret"], 3, labels=["down", "mid", "up"]))
    g = w.groupby("tercile", observed=True)
    return pd.DataFrame(
        {
            "ret_lo": g["sol_ret"].min(),
            "ret_hi": g["sol_ret"].max(),
            "n": g.size(),
            "X_median": g["X_W"].median(),
            "X_mean": g["X_W"].mean(),
            "share_neg": g["X_W"].apply(share_negative),
        }
    ).reset_index()


def run_perp(
    cfg: Config,
    variant: str,
    funding: pd.DataFrame,
    staking: pd.DataFrame,
    hurdle: pd.DataFrame,
    lending: pd.DataFrame,
    spot_close: pd.Series,
    q_runup: float,
) -> PerpResult:
    long, source = PERP_VARIANTS[variant]
    T = cfg.windows.T_lst_h if long == "lst" else cfg.windows.T_native_h
    beta = beta_p(q_runup, cfg.margin.perp_mm)
    k = k_p(cfg.margin.perp_im, beta)
    cost = perp_cost(cfg)
    ycol = "y_lst" if long == "lst" else "y_native"
    src = cfg.lending.reserve_yield_source
    r_res: pd.Series | float = 0.0
    if src != "none":
        col = f"r_res_{src}"
        r_res = lending[col] if col in lending.columns else 0.0
    hourly = hourly_carry(
        funding["f_h"] if "f_h" in funding.columns else pd.Series(dtype="float64"),
        staking[ycol] if ycol in staking.columns else pd.Series(dtype="float64"),
        hurdle["r_f"] if "r_f" in hurdle.columns else pd.Series(dtype="float64"),
        r_res,
        beta,
        k,
        cost,
    )
    windows = rolling_windows(hourly, cfg.windows.rolling_days)
    if not windows.empty:
        windows["sol_ret"] = trailing_return(spot_close, 30, windows["end"])
    res = PerpResult(variant, long, source, T, q_runup, beta, k, cost, hourly, windows)
    w30 = windows[windows["window_days"] == 30]["X_W"] if not windows.empty else pd.Series()
    r30 = windows[windows["window_days"] == 30]["R_W"] if not windows.empty else pd.Series()
    sample_days = (
        (hourly.index.max() - hourly.index.min()).total_seconds() / 86400 + 1 / 24
        if not hourly.empty
        else 0.0
    )
    res.summary = {
        "variant": variant,
        "kind": "P",
        "metric": "30d rolling X_W",
        "excess": describe(w30),
        "share_negative": share_negative(w30),
        "neg_run_days": longest_underwater_days(hourly["x_h"]) if not hourly.empty else 0.0,
        "R_median": float(r30.median()) if len(r30) else float("nan"),
        "R_p25": float(r30.quantile(0.25)) if len(r30) else float("nan"),
        "sample_days": sample_days,
        "sample_start": hourly.index.min().isoformat() if not hourly.empty else None,
        "sample_end": hourly.index.max().isoformat() if not hourly.empty else None,
        "uses_lst": long == "lst",
        "T_h": T,
        "q_runup": q_runup,
        "beta": beta,
        "k": k,
        "cost": cost,
        "components": {
            "y": float(hourly["y"].mean()) if not hourly.empty else float("nan"),
            "funding": float(hourly["funding_ann"].mean()) if not hourly.empty else float("nan"),
            "cost": -cost,
            "reserve_yield": float((beta * hourly["r_res"]).mean()) if not hourly.empty else 0.0,
            "r_f": float(hourly["r_f"].mean()) if not hourly.empty else float("nan"),
        },
        "flags": ["PROXY"] if source == "binance" else [],
    }
    res.regime = regime_split(windows) if not windows.empty else pd.DataFrame()
    return res


def funding_diagnostics(f_h: pd.Series, name: str) -> dict[str, Any]:
    from .stats import longest_negative_run_days

    s = f_h.dropna()
    ann = s * HOURS_PER_YEAR
    if ann.empty:
        return {"source": name, "hours": 0}
    return {
        "source": name,
        "hours": int(len(ann)),
        "start": ann.index.min().isoformat(),
        "end": ann.index.max().isoformat(),
        "share_positive": float((ann > 0).mean()),
        "median_annual": float(ann.median()),
        "mean_annual": float(ann.mean()),
        "p5_annual": float(ann.quantile(0.05)),
        "p95_annual": float(ann.quantile(0.95)),
        "longest_negative_run_days_7d": longest_negative_run_days(ann, 7),
    }
