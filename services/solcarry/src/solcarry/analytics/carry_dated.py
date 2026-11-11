"""Variant D: Binance COIN-M inverse quarterly short, held to expiry and rolled.

    b_t = F_t / I_t - 1
    a_t = ln(F_t / I_t) * 365 / dte_days
    basis_cycle = ln(F_entry / I_entry),  years_cycle = dte_entry / 365

D1-BASIS    all SOL posted as margin, nothing staked:   R = a - c_D
D2-STATIC   margin sized for q_alpha(U_2160):            R = a + (1 - phi_s) y - c_D
D2-MANAGED  margin sized for q_alpha(U_T_transfer):      R = a + (1 - phi_m) y - c_D   (optimistic)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..config import Config
from .reserve import margin_fraction
from .stats import describe, share_negative

DATED_VARIANTS = ("D1-BASIS", "D2-STATIC", "D2-MANAGED")
MIN_STAKING_COVERAGE = 0.9


def basis_frame(quarterly: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """Join quarterly closes with the index close; add b_t and a_t."""
    if quarterly.empty or index.empty:
        return pd.DataFrame(
            columns=["ts", "symbol", "expiry_ts", "close", "dte_days", "index", "b", "a"]
        )
    idx = index["close"].rename("index")
    q = quarterly.merge(idx, left_on="ts", right_index=True, how="left")
    q = q.dropna(subset=["close", "index"])
    q = q[q["dte_days"] > 0]
    q["b"] = q["close"] / q["index"] - 1.0
    q["a"] = np.log(q["close"] / q["index"]) * 365.0 / q["dte_days"]
    return q.reset_index(drop=True)


def entry_distribution(basis: pd.DataFrame, dte_min: float, dte_max: float) -> pd.DataFrame:
    return basis[(basis["dte_days"] >= dte_min) & (basis["dte_days"] <= dte_max)].copy()


def dated_cost(cfg: Config, years: float, with_lst: bool) -> float:
    c = cfg.costs
    bps = c.coinm_fee_bps * 2 + c.roll_slippage_bps + (c.lst_swap_bps if with_lst else 0.0)
    return bps / 1e4 / years if years > 0 else float("nan")


def build_cycles(basis: pd.DataFrame, dte_max: float, now: pd.Timestamp) -> pd.DataFrame:
    """One hold-to-expiry cycle per contract, entered when the previous one expires.

    The first contract (no predecessor) is entered at the first hour with
    ``dte <= dte_max``. Later contracts are entered at the first bar at or after
    the previous expiry (or at listing if listed later).
    """
    if basis.empty:
        return pd.DataFrame()
    contracts = (
        basis.groupby("symbol")["expiry_ts"].first().sort_values().reset_index().to_dict("records")
    )
    rows = []
    prev_exp: pd.Timestamp | None = None
    for c in contracts:
        sym, exp = c["symbol"], pd.Timestamp(c["expiry_ts"])
        g = basis[basis["symbol"] == sym].sort_values("ts")
        cand = g[g["ts"] >= prev_exp] if prev_exp is not None else g[g["dte_days"] <= dte_max]
        prev_exp = exp
        if cand.empty:
            continue
        e = cand.iloc[0]
        dte = float(e["dte_days"])
        rows.append(
            {
                "contract": sym,
                "entry_ts": pd.Timestamp(e["ts"]),
                "expiry_ts": exp,
                "dte_entry": dte,
                "F_entry": float(e["close"]),
                "I_entry": float(e["index"]),
                "basis_cycle": float(np.log(e["close"] / e["index"])),
                "years_cycle": dte / 365.0,
                "status": "expired" if exp <= now else "open",
            }
        )
    cyc = pd.DataFrame(rows)
    if cyc.empty:
        return cyc
    cyc["a_cycle"] = cyc["basis_cycle"] / cyc["years_cycle"]
    return cyc


def _mean_over(s: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> tuple[float, float]:
    """Mean of an hourly series over [start, end) and the fraction of hours observed."""
    hours = max(1, int((end - start) / pd.Timedelta(hours=1)))
    w = s[(s.index >= start) & (s.index < end)].dropna()
    return (float(w.mean()) if len(w) else float("nan"), len(w) / hours)


@dataclass
class DatedResult:
    entries: pd.DataFrame
    cycles: pd.DataFrame  # one row per (variant, contract)
    phi: dict[str, float]
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)


def run_dated(
    cfg: Config,
    quarterly: pd.DataFrame,
    index: pd.DataFrame,
    staking: pd.DataFrame,
    hurdle: pd.DataFrame,
    q_hold: float,
    q_transfer: float,
    now: pd.Timestamp | None = None,
) -> DatedResult:
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    basis = basis_frame(quarterly, index)
    entries = entry_distribution(basis, cfg.dated.dte_min, cfg.dated.dte_max)
    cyc = build_cycles(basis, cfg.dated.dte_max, now)
    im, mm = cfg.margin.coinm_im, cfg.margin.coinm_mm
    phis = {
        "D1-BASIS": 1.0,
        "D2-STATIC": margin_fraction(im, q_hold, mm),
        "D2-MANAGED": margin_fraction(im, q_transfer, mm),
    }
    y = staking["y_lst"] if "y_lst" in staking.columns else pd.Series(dtype="float64")
    rf = hurdle["r_f"] if "r_f" in hurdle.columns else pd.Series(dtype="float64")

    rows = []
    for _, c in cyc.iterrows() if not cyc.empty else iter([]):
        end = min(c["expiry_ts"], now)
        y_mean, y_cov = _mean_over(y, c["entry_ts"], end)
        rf_mean, _ = _mean_over(rf, c["entry_ts"], end)
        for v in DATED_VARIANTS:
            if v not in cfg.variants:
                continue
            staked = 1.0 - phis[v]
            uses_lst = v != "D1-BASIS"
            cost = dated_cost(cfg, c["years_cycle"], uses_lst)
            stake_contrib = staked * y_mean if uses_lst else 0.0
            has_staking = (not uses_lst) or (y_cov >= MIN_STAKING_COVERAGE)
            R = c["a_cycle"] + (stake_contrib if has_staking else np.nan) - cost
            rows.append(
                {
                    "variant": v,
                    **c.to_dict(),
                    "staked_fraction": staked,
                    "y_mean": y_mean,
                    "staking_coverage": y_cov,
                    "staking_contrib": stake_contrib if has_staking else np.nan,
                    "cost": cost,
                    "R": R,
                    "r_f": rf_mean,
                    "X": R - rf_mean,
                    "scored": bool(c["status"] == "expired" and has_staking and np.isfinite(R)),
                }
            )
    cycles = pd.DataFrame(rows)
    res = DatedResult(entries=entries, cycles=cycles, phi=phis)
    for v in DATED_VARIANTS:
        if v not in cfg.variants:
            continue
        sub = cycles[(cycles["variant"] == v) & cycles["scored"]] if not cycles.empty else cycles
        days = float(sub["years_cycle"].sum() * 365) if len(sub) else 0.0
        X = sub["X"] if len(sub) else pd.Series(dtype="float64")
        R = sub["R"] if len(sub) else pd.Series(dtype="float64")
        flags = []
        if v == "D2-MANAGED":
            flags.append("OPTIMISTIC")
        res.summaries[v] = {
            "variant": v,
            "kind": "D",
            "metric": "per-cycle X",
            "excess": describe(X),
            "share_negative": share_negative(X),
            "neg_run_days": None,
            "R_median": float(R.median()) if len(R) else float("nan"),
            "R_p25": float(R.quantile(0.25)) if len(R) else float("nan"),
            "sample_days": days,
            "cycles": int(len(sub)),
            "sample_start": sub["entry_ts"].min().isoformat() if len(sub) else None,
            "sample_end": sub["expiry_ts"].max().isoformat() if len(sub) else None,
            "uses_lst": v != "D1-BASIS",
            "phi": phis[v],
            "staked_fraction": 1.0 - phis[v],
            "flags": flags,
        }
    return res


def entry_quantiles(entries: pd.DataFrame) -> pd.DataFrame:
    if entries.empty:
        return pd.DataFrame()
    qs = [0.05, 0.25, 0.5, 0.75, 0.95]
    pooled = {
        "symbol": "POOLED",
        "n": len(entries),
        **{f"q{q:g}": entries["a"].quantile(q) for q in qs},
    }
    rows = [pooled]
    for sym, g in entries.groupby("symbol"):
        rows.append({"symbol": sym, "n": len(g), **{f"q{q:g}": g["a"].quantile(q) for q in qs}})
    return pd.DataFrame(rows)
