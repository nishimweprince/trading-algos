"""Pipeline steps shared by the CLI: fetch, normalize, validate, analyze, gate, report, depth."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from . import EVIDENCE_TIER
from .analytics import carry_dated, carry_perp, depth, runups
from .analytics.sizing import sizing_table
from .config import Config, Paths, config_hash
from .gate import scorecard
from .http import Http
from .lock import LockStatus, check_lock
from .normalize.build import Tables, build_tables
from .normalize.funding import HOURS_PER_YEAR
from .sources.binance_vision import BinanceVision
from .sources.defillama import DefiLlama
from .sources.fred import Fred
from .sources.hyperliquid import Hyperliquid
from .sources.jupiter import Jupiter
from .sources.okx import Okx
from .sources.velocity import Velocity
from .validate import ValidationResult, run_validation

log = logging.getLogger(__name__)
SOURCES = ("velocity", "binance", "hyperliquid", "defillama", "fred", "jupiter", "okx")


def _json_default(o: Any) -> Any:
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def _clean(o: Any) -> Any:
    """Replace NaN/inf with None so JSON stays valid for browsers."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    return o


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(_clean(obj), indent=2, default=_json_default) + "\n")


# --------------------------------------------------------------------------- runs
def new_run(cfg: Config, paths: Paths) -> Path:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + config_hash(cfg)[:8]
    d = paths.results / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.dump(), sort_keys=False))
    return d


def latest_run(paths: Paths, require: str = "summaries.json") -> Path | None:
    if not paths.results.exists():
        return None
    runs = sorted(p for p in paths.results.iterdir() if p.is_dir() and (p / require).exists())
    return runs[-1] if runs else None


def resolve_run(paths: Paths, run_id: str | None, require: str = "summaries.json") -> Path:
    if run_id:
        d = paths.results / run_id
        if not d.exists():
            raise FileNotFoundError(f"no run {run_id} under {paths.results}")
        return d
    latest = latest_run(paths, require)
    if latest is None:
        raise FileNotFoundError(f"no run with {require}; run `solcarry analyze` first")
    return latest


# --------------------------------------------------------------------------- fetch
def fetch(cfg: Config, paths: Paths, source: str = "all") -> dict[str, Any]:
    paths.ensure()
    wanted = SOURCES if source == "all" else (source,)
    out: dict[str, Any] = {}
    with Http(cfg.http) as http:
        for s in wanted:
            log.info("fetch: %s", s)
            if s == "velocity":
                out[s] = len(Velocity(cfg, http, paths.raw).sync())
            elif s == "binance":
                out[s] = {
                    k: len(v) for k, v in BinanceVision(cfg, http, paths.raw).sync_all().items()
                }
            elif s == "hyperliquid":
                out[s] = Hyperliquid(cfg, http, paths.raw).sync()
            elif s == "defillama":
                out[s] = DefiLlama(cfg, http, paths.raw).sync()
            elif s == "fred":
                out[s] = Fred(cfg, http, paths.raw).sync()
            elif s == "jupiter":
                snap = depth.snapshot(cfg, Jupiter(cfg, http))
                depth.append_history(snap, paths.results)
                out[s] = len(snap)
            elif s == "okx":
                try:
                    out[s] = Okx(cfg, http, paths.raw).sync()
                except Exception as e:  # optional cross-check: never fatal
                    log.warning("okx cross-check failed: %s", e)
                    out[s] = f"failed: {e}"
            else:
                raise ValueError(f"unknown source {s}")
    return out


def normalize(cfg: Config, paths: Paths) -> Tables:
    paths.ensure()
    t = build_tables(cfg, paths)
    t.save(paths.normalized)
    return t


def validate(cfg: Config, paths: Paths, run_dir: Path) -> ValidationResult:
    t = Tables.load(paths.normalized)
    res = run_validation(cfg, t)
    write_json(run_dir / "validation.json", res.to_json())
    return res


# --------------------------------------------------------------------------- analyze
def analyze(
    cfg: Config, paths: Paths, run_dir: Path, variants: list[str] | None = None
) -> dict[str, Any]:
    t = Tables.load(paths.normalized)
    variants = variants or list(cfg.variants)
    alpha = cfg.windows.gate_alpha
    now = pd.Timestamp.now(tz="UTC")

    # Run-ups -------------------------------------------------------------
    windows = sorted(
        set(cfg.windows.runup_windows_h)
        | {
            cfg.windows.T_lst_h,
            cfg.windows.T_native_h,
            cfg.windows.T_transfer_h,
            cfg.windows.T_hold_h,
        }
    )
    U = runups.runups(t.spot, windows)
    rtab = runups.runup_table(U, cfg.windows.alpha)
    rtab.to_csv(run_dir / "runups.csv", index=False)
    cdf_grid = np.linspace(0, 1, 201)
    cdf = {
        str(int(c.split("_")[1])): [float(x) for x in U[c].dropna().quantile(cdf_grid)]
        for c in U.columns
    }

    def q(T: int, a: float = alpha) -> float:
        return runups.q_alpha(rtab, T, a)

    # Funding diagnostics ----------------------------------------------------
    funding_tables = {
        "velocity": t.funding_velocity,
        "binance": t.funding_binance,
        "hyperliquid": t.funding_hyperliquid,
    }
    diag = []
    daily: dict[str, pd.Series] = {}
    floor = cfg.velocity.funding_floor_annual
    for name, ft in funding_tables.items():
        f_h = ft["f_h"] if "f_h" in ft.columns else pd.Series(dtype=float)
        dg = carry_perp.funding_diagnostics(f_h, name)
        ann = (f_h * HOURS_PER_YEAR).dropna()
        if name != "binance" and not ann.empty:
            # Velocity and Hyperliquid both have a 0.01%/8h interest baseline (10.95%/yr).
            dg["share_at_floor"] = float(((ann - floor).abs() <= 1e-4).mean())
        diag.append(dg)
        daily[name] = (
            (f_h * HOURS_PER_YEAR).resample("D").mean() if len(f_h) else pd.Series(dtype=float)
        )
    pd.DataFrame(diag).to_csv(run_dir / "funding_diagnostics.csv", index=False)

    fund_daily = pd.DataFrame(daily)
    for name in funding_tables:
        fund_daily[f"{name}_7d"] = fund_daily[name].rolling(7, min_periods=3).mean()
    fund_daily.index.name = "ts"
    fund_daily.to_parquet(run_dir / "funding_daily.parquet")

    summaries: dict[str, dict[str, Any]] = {}
    reserves: dict[str, Any] = {"alpha": alpha}

    # Variant P ---------------------------------------------------------------
    pw, comps, regimes = [], [], []
    gates: list[dict[str, Any]] = []
    for v in [x for x in variants if x in carry_perp.PERP_VARIANTS]:
        long, source = carry_perp.PERP_VARIANTS[v]
        T = cfg.windows.T_lst_h if long == "lst" else cfg.windows.T_native_h
        funding = funding_tables[source]
        res = carry_perp.run_perp(
            cfg, v, funding, t.staking, t.hurdle, t.lending, t.spot["close"], q(T)
        )
        summaries[v] = res.summary
        reserves[v] = {"T_h": T, "q_runup": res.q_runup, "beta_P": res.beta, "k_P": res.k}
        if res.summary.get("gated"):
            gates.append({"variant": v, **res.summary["gating"]})
        if not res.windows.empty:
            pw.append(res.windows.assign(variant=v))
        if not res.regime.empty:
            regimes.append(res.regime.assign(variant=v))
        if not res.hourly.empty:
            d = res.hourly[["y", "funding_ann", "r_f", "x_h", "R_h"]].resample("D").mean()
            d["cost"] = -res.cost
            d["variant"] = v
            comps.append(d.dropna(subset=["x_h"]))
    perp_windows = pd.concat(pw, ignore_index=True) if pw else pd.DataFrame()
    perp_windows.to_parquet(run_dir / "perp_windows.parquet")
    (pd.concat(regimes, ignore_index=True) if regimes else pd.DataFrame()).to_csv(
        run_dir / "perp_regime.csv", index=False
    )
    pd.DataFrame(gates).to_csv(run_dir / "perp_gating.csv", index=False)
    comp_df = pd.concat(comps) if comps else pd.DataFrame()
    comp_df.index.name = "ts"
    comp_df.to_parquet(run_dir / "perp_components_daily.parquet")

    # Variant D ---------------------------------------------------------------
    if any(v in carry_dated.DATED_VARIANTS for v in variants):
        dres = carry_dated.run_dated(
            cfg,
            t.quarterly,
            t.index,
            t.staking,
            t.hurdle,
            q(cfg.windows.T_hold_h),
            q(cfg.windows.T_transfer_h),
            now,
        )
        dres.cycles.to_csv(run_dir / "dated_cycles.csv", index=False)
        dres.entries.to_parquet(run_dir / "dated_entries.parquet")
        carry_dated.entry_quantiles(dres.entries).to_csv(
            run_dir / "dated_entry_quantiles.csv", index=False
        )
        for v, s in dres.summaries.items():
            if v in variants:
                summaries[v] = s
        reserves["coinm"] = {
            "q_U_hold": q(cfg.windows.T_hold_h),
            "q_U_transfer": q(cfg.windows.T_transfer_h),
            "phi": dres.phi,
            "mm": cfg.margin.coinm_mm,
            "im": cfg.margin.coinm_im,
        }

    sizing = sizing_table(list(summaries.values()), cfg.sizing.target_per_quarter_usd)
    sizing.to_csv(run_dir / "sizing.csv", index=False)

    coverage = {}
    for name in (
        "spot",
        "funding_velocity",
        "funding_binance",
        "funding_hyperliquid",
        "index",
        "staking",
        "hurdle",
        "lending",
    ):
        df: pd.DataFrame = getattr(t, name)
        coverage[name] = {
            "rows": len(df),
            "start": df.index.min().isoformat() if len(df) else None,
            "end": df.index.max().isoformat() if len(df) else None,
        }
    coverage["quarterly"] = {
        "contracts": sorted(t.quarterly["symbol"].unique().tolist()) if len(t.quarterly) else [],
        "rows": len(t.quarterly),
    }

    out = {
        "evidence_tier": EVIDENCE_TIER,
        "generated_at": now.isoformat(),
        "variants": variants,
        "gate_alpha": alpha,
        "summaries": summaries,
        "reserves": reserves,
        "funding_diagnostics": diag,
        "runup_cdf": cdf,
        "coverage": coverage,
    }
    write_json(run_dir / "summaries.json", out)
    return out


# --------------------------------------------------------------------------- gate
def gate(cfg: Config, paths: Paths, run_dir: Path, lock: LockStatus) -> dict[str, Any]:
    summ = json.loads((run_dir / "summaries.json").read_text())
    hist = depth.load_history(paths.results)
    impact, impact_ts = depth.latest_impact(hist, cfg.depth.gate_lst, cfg.depth.target_size)
    vflags: dict[str, Any] = {}
    vpath = run_dir / "validation.json"
    if vpath.exists():
        vflags = json.loads(vpath.read_text())
    short_vel = any(
        c["name"] == "velocity_sample" and not c["ok"] for c in vflags.get("checks", [])
    )
    scores = []
    for v in summ["variants"]:
        s = summ["summaries"].get(v)
        if s is None:
            continue
        scores.append(
            scorecard.score_variant(
                s,
                cfg.thresholds,
                impact if s.get("uses_lst") else None,
                short_sample_flag=short_vel and v.endswith("-VEL"),
            )
        )
    result = scorecard.overall(scores)
    pd.DataFrame([s.to_row() for s in scores]).to_csv(run_dir / "scorecard.csv", index=False)
    g = {
        "result": result,
        "lock": {
            "state": lock.state,
            "ok": lock.ok,
            "detail": lock.detail,
            "locked_at": lock.locked_at,
        },
        "unlocked": not lock.ok,
        "depth_impact": impact,
        "depth_impact_ts": impact_ts,
        "depth_target": {"lst": cfg.depth.gate_lst, "size": cfg.depth.target_size},
        "scores": scorecard.scores_to_json(scores),
        "evidence_tier": EVIDENCE_TIER,
    }
    write_json(run_dir / "gate.json", g)
    return g


def depth_snapshot(cfg: Config, paths: Paths, run_dir: Path | None = None) -> pd.DataFrame:
    paths.ensure()
    with Http(cfg.http) as http:
        snap = depth.snapshot(cfg, Jupiter(cfg, http))
    depth.append_history(snap, paths.results)
    if run_dir is not None:
        snap.to_csv(run_dir / "depth.csv", index=False)
    return snap


def lock_status(cfg: Config, paths: Paths) -> LockStatus:
    return check_lock(cfg, paths.lock_file)
