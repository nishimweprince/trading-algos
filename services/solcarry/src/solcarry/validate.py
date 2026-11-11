"""Data sanity checks (spec section 4.4). Hard failures abort the run (exit code 2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import Config
from .normalize.build import Tables
from .normalize.funding import HOURS_PER_YEAR
from .normalize.time import missing_hours


@dataclass
class Check:
    name: str
    level: str  # hard | soft
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    checks: list[Check]

    @property
    def hard_failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "hard" and not c.ok]

    @property
    def ok(self) -> bool:
        return not self.hard_failed

    def flag(self, name: str) -> bool:
        return any(c.name == name and not c.ok for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}


def check_velocity_units(rec: pd.DataFrame, tol: float) -> Check:
    name = "velocity_unit"
    if rec.empty:
        return Check(name, "hard", True, "no Velocity records; check skipped", {"skipped": True})
    r = rec.sort_values("recordId")
    consecutive = r["recordId"].diff() == 1
    dcum = r["cumulativeFundingRateLong"].diff()
    err = (dcum - r["fundingRateLong"]).abs()[consecutive]
    n = int(consecutive.sum())
    bad = int((err > tol).sum())
    ann_conv = float(np.nanmedian(r["fundingRate"] / r["oraclePriceTwap"]) * HOURS_PER_YEAR)
    ann_raw = float(np.nanmedian(r["fundingRate"]) * HOURS_PER_YEAR)
    band = (-2.0, 2.0)
    conv_ok = band[0] <= ann_conv <= band[1]
    raw_ok = band[0] <= ann_raw <= band[1]
    data = {
        "pairs_checked": n,
        "cum_mismatches": bad,
        "max_abs_err": float(err.max()) if n else None,
        "median_annual_converted": ann_conv,
        "median_annual_raw": ann_raw,
    }
    if n and bad:
        return Check(
            name,
            "hard",
            False,
            f"diff(cumulativeFundingRateLong) != fundingRateLong on {bad}/{n} consecutive records",
            data,
        )
    if not conv_ok and raw_ok:
        return Check(
            name,
            "hard",
            False,
            "UNIT ASSUMPTION WRONG: fundingRate/oraclePriceTwap annualizes outside ±200%/yr "
            f"({ann_conv:.2%}) while raw fundingRate is in band ({ann_raw:.2%}). "
            "fundingRate looks like a fraction, not USD per SOL.",
            data,
        )
    if not conv_ok:
        return Check(name, "hard", False, f"converted median {ann_conv:.2%}/yr outside ±200%", data)
    return Check(
        name,
        "hard",
        True,
        f"{n} cumulative diffs match; median converted {ann_conv:.2%}/yr",
        data,
    )


CONVERGENCE_WINDOW_MIN = 30
LAST_TRADE_MAX_GAP_MIN = 15


def _minute_convergence(m: pd.DataFrame, exp: pd.Timestamp) -> dict[str, Any]:
    """Expiry checks on 1m bars of one contract.

    Binance settles COIN-M quarterlies on the average index over the final 30 minutes,
    and the last trade of an illiquid contract can sit 0.5% away from the index, so a
    single hourly close cannot test convergence. Instead: median of minute-matched
    F/I - 1 over traded minutes in the final 30 minutes, plus "trading stops at expiry"
    (no volume at/after the assumed expiry, last trade within 15 minutes before it).
    """
    m = m.set_index("ts").sort_index()
    traded = m[m["f_volume"] > 0]
    pre = traded[traded.index < exp]
    after = traded[traded.index >= exp]
    win = pre[pre.index >= exp - pd.Timedelta(minutes=CONVERGENCE_WINDOW_MIN)]
    if win.empty:  # thin contract: widen to the final hour
        win = pre[pre.index >= exp - pd.Timedelta(minutes=60)]
    dev = (win["f_close"] / win["i_close"] - 1).dropna()
    last = pre.index.max() if len(pre) else None
    return {
        "method": "1m",
        "last_trade": last.isoformat() if last is not None else None,
        "trades_after_expiry": int(len(after)),
        "window_minutes_traded": int(len(dev)),
        "deviation": float(dev.median()) if len(dev) else float("nan"),
        "hour_ok": bool(
            len(after) == 0
            and last is not None
            and exp - last <= pd.Timedelta(minutes=LAST_TRADE_MAX_GAP_MIN)
        ),
    }


def _hourly_convergence(g: pd.DataFrame, idx_close: pd.Series, exp: pd.Timestamp) -> dict[str, Any]:
    """Fallback when 1m data is missing: final hourly close before expiry vs index.

    A bar at/after expiry is tolerated only if it repeats the last pre-expiry close
    (a flat settlement bar with no trades).
    """
    g = g.set_index("ts").sort_index()
    pre = g[g.index < exp]
    post = g[g.index >= exp]
    if pre.empty:
        return {
            "method": "1h",
            "last_trade": None,
            "trades_after_expiry": int(len(post)),
            "window_minutes_traded": None,
            "deviation": float("nan"),
            "hour_ok": False,
        }
    conv_ts = pre.index.max()
    f_last = float(pre["close"].iloc[-1])
    i_last = float(idx_close.get(conv_ts, np.nan))
    flat_post = bool((post["close"] == f_last).all())
    return {
        "method": "1h",
        "last_trade": conv_ts.isoformat(),
        "trades_after_expiry": 0 if flat_post else int(len(post)),
        "window_minutes_traded": None,
        "deviation": f_last / i_last - 1 if np.isfinite(i_last) else float("nan"),
        "hour_ok": bool(conv_ts == exp - pd.Timedelta(hours=1) and flat_post),
    }


def check_convergence(
    quarterly: pd.DataFrame,
    index: pd.DataFrame,
    tol: float,
    now: pd.Timestamp,
    expiry_1m: pd.DataFrame | None = None,
) -> Check:
    name = "quarterly_convergence"
    if quarterly.empty:
        return Check(name, "hard", True, "no quarterly data; skipped", {"skipped": True})
    idx_close = index["close"] if "close" in index.columns else pd.Series(dtype="float64")
    rows, fails = [], []
    for sym, g in quarterly.groupby("symbol"):
        exp = pd.Timestamp(g["expiry_ts"].iloc[0])
        if exp > now:
            continue
        m = (
            expiry_1m[expiry_1m["symbol"] == sym]
            if expiry_1m is not None and not expiry_1m.empty
            else pd.DataFrame()
        )
        r = _minute_convergence(m, exp) if not m.empty else _hourly_convergence(g, idx_close, exp)
        dev = r["deviation"]
        rows.append({"symbol": sym, "expiry": exp.isoformat(), **r})
        if not (np.isfinite(dev) and abs(dev) < tol):
            fails.append(f"{sym}: |F/I-1| = {abs(dev):.3%} ({r['method']})")
        if not r["hour_ok"]:
            fails.append(
                f"{sym}: trading does not stop at the assumed expiry {exp:%H:%M} UTC "
                f"(last trade {r['last_trade']}, {r['trades_after_expiry']} trades after)"
            )
    ok = not fails
    worst = max((abs(r["deviation"]) for r in rows if np.isfinite(r["deviation"])), default=0.0)
    detail = (
        f"{len(rows)} expired contracts converge within {tol:.2%} (worst {worst:.3%}); "
        "trading stops at the assumed expiry hour"
        if ok
        else "; ".join(fails)
    )
    return Check(name, "hard", ok, detail, {"contracts": rows})


def check_grid(tables: Tables, now: pd.Timestamp) -> Check:
    problems = []
    for name in ("spot", "funding_velocity", "funding_binance", "index", "staking", "hurdle"):
        df: pd.DataFrame = getattr(tables, name)
        ix = df.index
        if len(ix) == 0:
            continue
        if not isinstance(ix, pd.DatetimeIndex) or str(ix.tz) != "UTC":
            problems.append(f"{name}: index not tz-aware UTC")
            continue
        if not ix.is_monotonic_increasing or not ix.is_unique:
            problems.append(f"{name}: index not monotonic/unique")
        if ix.max() > now:
            problems.append(f"{name}: future timestamps (max {ix.max()})")
        if (ix != ix.floor("h")).any():
            problems.append(f"{name}: timestamps not on the hour")
    return Check(
        "grid_integrity",
        "hard",
        not problems,
        "all hourly tables monotonic, unique, UTC" if not problems else "; ".join(problems),
    )


def check_gaps(tables: Tables) -> Check:
    data = {}
    for name, col in (
        ("spot", "close"),
        ("funding_velocity", "f_h"),
        ("funding_binance", "f_h"),
        ("index", "close"),
        ("staking", "y_lst"),
        ("hurdle", "r_f"),
    ):
        df: pd.DataFrame = getattr(tables, name)
        if df.empty or col not in df.columns:
            data[name] = {"rows": 0, "missing_hours": None, "longest_gap_h": None}
            continue
        miss, longest = missing_hours(df[col])
        data[name] = {
            "rows": len(df),
            "start": df.index.min().isoformat(),
            "end": df.index.max().isoformat(),
            "missing_hours": miss,
            "longest_gap_h": longest,
        }
    worst = max((v["longest_gap_h"] or 0) for v in data.values())
    return Check("gaps", "soft", worst <= 72, f"longest gap across sources {worst} h", data)


def check_binance_interval(fb: pd.DataFrame) -> Check:
    if fb.empty or "interval_h" not in fb.columns:
        return Check("binance_funding_interval", "soft", True, "no data")
    iv = fb["interval_h"].dropna()
    off = iv[iv != 8]
    periods = []
    if not off.empty:
        grp = (off.index.to_series().diff() != pd.Timedelta(hours=1)).cumsum()
        for _, g in off.groupby(grp):
            periods.append(
                {
                    "start": g.index.min().isoformat(),
                    "end": g.index.max().isoformat(),
                    "interval_h": int(g.iloc[0]),
                }
            )
    return Check(
        "binance_funding_interval",
        "soft",
        not periods,
        f"{len(periods)} periods with interval != 8h",
        {"periods": periods},
    )


def check_velocity_floor(fv: pd.DataFrame, floor: float) -> Check:
    if fv.empty:
        return Check("velocity_floor", "soft", True, "no data", {"share_at_floor": None})
    ann = fv["f_h"].dropna() * HOURS_PER_YEAR
    share = float(((ann - floor).abs() <= 1e-4).mean()) if len(ann) else 0.0
    ok = share < 0.5
    msg = f"{share:.1%} of hours within 1 bp/yr of the {floor:.2%} floor"
    if not ok:
        msg = "FUNDING PINNED AT FLOOR: " + msg
    return Check("velocity_floor", "soft", ok, msg, {"share_at_floor": share})


def check_velocity_sample(fv: pd.DataFrame, min_days: float) -> Check:
    if fv.empty:
        return Check(
            "velocity_sample", "soft", False, "SHORT_SAMPLE: no Velocity data", {"days": 0}
        )
    days = (fv.index.max() - fv.index.min()).total_seconds() / 86400
    ok = days >= min_days
    return Check(
        "velocity_sample",
        "soft",
        ok,
        f"{days:.0f} days of Velocity history" + ("" if ok else " (SHORT_SAMPLE)"),
        {"days": days},
    )


def run_validation(cfg: Config, t: Tables, now: pd.Timestamp | None = None) -> ValidationResult:
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    checks = [
        check_velocity_units(t.velocity_records, cfg.validate_.velocity_cum_tol),
        check_convergence(t.quarterly, t.index, cfg.validate_.convergence_tol, now, t.expiry_1m),
        check_grid(t, now),
        check_gaps(t),
        check_binance_interval(t.funding_binance),
        check_velocity_floor(t.funding_velocity, cfg.velocity.funding_floor_annual),
        check_velocity_sample(t.funding_velocity, 180),
    ]
    return ValidationResult(checks)
