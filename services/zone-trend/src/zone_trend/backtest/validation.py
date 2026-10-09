"""Holdout, ablations and the acceptance table (the M4 gate).

- ``run_holdout``: the last 9 months, once. It uses the last walk-forward
  window's chosen parameters and displacement table, and refuses to run again
  (a second look at the holdout makes it in-sample).
- ``run_ablation``: the default config against one component switched off,
  both over the walk-forward's out-of-sample span with no optimisation. A
  component "adds" when the default has the higher expectancy.
- ``acceptance``: every criterion in the spec, with value, threshold and
  pass / fail / pending.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from zone_trend.backtest.engine import RunSpec, run
from zone_trend.backtest.metrics import summary
from zone_trend.backtest.montecarlo import monte_carlo
from zone_trend.backtest.walkforward import candidates
from zone_trend.config import Config
from zone_trend.data.store import DataPaths
from zone_trend.risk.displacement import DisplacementTable

__all__ = ["ABLATIONS", "acceptance", "run_ablation", "run_holdout"]

ABLATIONS: dict[str, tuple[str, str, object]] = {
    "volume_filter": ("entries", "volume_mult", 0.0),
    "fib_band": ("entries", "fib_band", [0.3, 0.8]),  # the spec band vs a plain band
    "slope": ("structure", "slope_gate", False),
    "funding": ("filters", "funding_max_8h", 0.01),
    "htf_zones": ("zones", "use_d1", False),
    "btc_gate": ("filters", "btc_gate", False),
}


def _load_wf(wf_dir: Path) -> dict:
    return json.loads((wf_dir / "wf.json").read_text())


def _universe(paths: DataPaths, meta: dict) -> dict | None:
    if not meta["universe_history"]:
        return None
    return json.loads((paths.root / "universe" / "history.json").read_text())["months"]


def run_holdout(paths: DataPaths, wf_dir: Path, force: bool = False) -> dict:
    out = wf_dir / "holdout"
    if (out / "summary.json").exists() and not force:
        raise SystemExit(f"holdout already run ({out}); it is run once. --force overrides.")
    meta = _load_wf(wf_dir)
    cfg = Config.model_validate(meta["config"])
    last = meta["windows"][-1]
    chosen = dict(candidates(cfg))[last["chosen"]]
    table = DisplacementTable(
        k={tuple(k.split("|")): v for k, v in last["k_sym"].items()}  # type: ignore[misc]
    )
    start, end = meta["holdout"]
    oos_eq = pd.read_parquet(wf_dir / "oos_equity.parquet")
    spec = RunSpec(
        start,
        end,
        symbols=meta["symbols"],
        universe=_universe(paths, meta),
        equity=float(oos_eq["equity"].iloc[-1]),
        displacement=table,
        workers=4,
    )
    res = run(paths, chosen, spec)
    out.mkdir(parents=True, exist_ok=True)
    res.trades.to_parquet(out / "trades.parquet", index=False)
    res.equity.to_parquet(out / "equity.parquet", index=False)
    stats = summary(res.trades, res.equity)
    stats["config_label"] = last["chosen"]
    (out / "summary.json").write_text(json.dumps(stats, indent=1, default=str))
    return stats


def _set(cfg: Config, section: str, name: str, value) -> Config:
    data = cfg.model_dump(mode="json")
    data[section][name] = value
    return Config.model_validate(data)


def run_ablation(paths: DataPaths, wf_dir: Path, component: str, workers: int = 4) -> dict:
    meta = _load_wf(wf_dir)
    cfg = Config.model_validate(meta["config"])
    section, name, value = ABLATIONS[component]
    windows = meta["windows"]
    span = (windows[0]["window"]["oos_start"], windows[-1]["window"]["oos_end"])
    tape_end = str(pd.Period(meta["holdout"][0], freq="M") - 1)
    universe = _universe(paths, meta)
    results = {}
    for label, c in (("default", cfg), ("ablated", _set(cfg, section, name, value))):
        spec = RunSpec(
            *span, symbols=meta["symbols"], universe=universe, workers=workers, tape_end=tape_end
        )
        res = run(paths, c, spec)
        results[label] = summary(res.trades, res.equity)
    d, a = results["default"], results["ablated"]
    entry = {
        "component": component,
        "change": f"{section}.{name} = {value}",
        "default": {k: d[k] for k in ("trades", "profit_factor", "expectancy_r")},
        "ablated": {k: a[k] for k in ("trades", "profit_factor", "expectancy_r")},
        "adds_expectancy": bool(d["expectancy_r"] > a["expectancy_r"]),
    }
    path = wf_dir / "ablations.json"
    all_ = json.loads(path.read_text()) if path.exists() else {}
    all_[component] = entry
    path.write_text(json.dumps(all_, indent=1, default=str))
    return entry


def _row(name: str, value, threshold: str, ok: bool | None) -> dict:
    status = "pending" if ok is None else ("pass" if ok else "fail")
    return {"criterion": name, "value": value, "threshold": threshold, "status": status}


def _num(x: float, fmt: str = ".3f") -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


def acceptance(wf_dir: Path, mc_paths: int = 10_000) -> dict:
    meta = _load_wf(wf_dir)
    oos_t = pd.read_parquet(wf_dir / "oos_trades.parquet")
    oos_e = pd.read_parquet(wf_dir / "oos_equity.parquet")
    st = summary(oos_t, oos_e)
    slip = summary(
        pd.read_parquet(wf_dir / "oos_slip2_trades.parquet"),
        pd.read_parquet(wf_dir / "oos_slip2_equity.parquet"),
    )
    bench = summary(
        pd.read_parquet(wf_dir / "benchmark_trades.parquet"),
        pd.read_parquet(wf_dir / "benchmark_equity.parquet"),
    )
    mc = monte_carlo(oos_t, oos_e, n_paths=mc_paths)
    delay_t = pd.read_parquet(wf_dir / "oos_delay1_trades.parquet")
    delay_e = pd.read_parquet(wf_dir / "oos_delay1_equity.parquet")
    mc_delay = monte_carlo(delay_t, delay_e, n_paths=mc_paths)

    per_sym = oos_t.groupby("symbol").size() if len(oos_t) else pd.Series(dtype=int)
    deep = int((per_sym >= 15).sum())
    exp_sym = oos_t.groupby("symbol")["r_mult"].mean() if len(oos_t) else pd.Series(dtype=float)
    edge_share = float((exp_sym > 0).mean()) if len(exp_sym) else math.nan
    nbrs = meta["robustness"][1:]
    robust = [n for n in nbrs if (n["profit_factor"] or 0) >= 1.15]
    robust_share = len(robust) / len(nbrs) if nbrs else math.nan
    mc_dd95 = mc.get("max_drawdown", {}).get(95, math.nan)

    rows = [
        _row(
            "OOS trades",
            f"{st['trades']} ({deep} symbols with >=15)",
            ">= 150, >= 15 on >= 8 symbols",
            st["trades"] >= 150 and deep >= 8,
        ),
        _row("Profit factor", _num(st["profit_factor"]), ">= 1.3", st["profit_factor"] >= 1.3),
        _row(
            "Profit factor at 2x slippage",
            _num(slip["profit_factor"]),
            ">= 1.15",
            slip["profit_factor"] >= 1.15,
        ),
        _row("Expectancy (R)", _num(st["expectancy_r"]), ">= +0.15", st["expectancy_r"] >= 0.15),
        _row("Max drawdown", _num(st["max_drawdown"]), "<= 0.25", st["max_drawdown"] <= 0.25),
        _row("Monte Carlo 95th pct drawdown", _num(mc_dd95), "<= 0.35", mc_dd95 <= 0.35),
        _row(
            "Sharpe (daily)", _num(st["sharpe_daily"], ".2f"), ">= 1.0", st["sharpe_daily"] >= 1.0
        ),
        _row(
            "Benchmark (EMA 20/100) Sharpe gap",
            f"{_num(st['sharpe_daily'], '.2f')} vs {_num(bench['sharpe_daily'], '.2f')}",
            ">= +0.2",
            (st["sharpe_daily"] - bench["sharpe_daily"]) >= 0.2
            if not math.isnan(bench["sharpe_daily"])
            else None,
        ),
        _row(
            "Robustness: ±20% neighbours with PF >= 1.15",
            _num(robust_share, ".0%"),
            ">= 70%",
            robust_share >= 0.7 if nbrs else None,
        ),
        _row(
            "Robustness: symbols with positive expectancy",
            _num(edge_share, ".0%"),
            ">= 70%",
            edge_share >= 0.7 if len(exp_sym) else None,
        ),
    ]

    hold = wf_dir / "holdout" / "summary.json"
    if hold.exists():
        h = json.loads(hold.read_text())
        ok = h["profit_factor"] >= 1.15 and h["max_drawdown"] <= 1.25 * st["max_drawdown"]
        rows.append(
            _row(
                "Holdout (last 9 months)",
                f"PF {_num(h['profit_factor'])}, DD {_num(h['max_drawdown'])}",
                "PF >= 1.15, DD <= 1.25 x WF DD",
                ok,
            )
        )
    else:
        rows.append(
            _row("Holdout (last 9 months)", "not run", "PF >= 1.15, DD <= 1.25 x WF DD", None)
        )
    abl = wf_dir / "ablations.json"
    if abl.exists():
        entries = json.loads(abl.read_text())
        bad = [k for k, v in entries.items() if not v["adds_expectancy"]]
        rows.append(
            _row(
                "Ablations: each filter adds OOS expectancy",
                f"{len(entries) - len(bad)}/{len(entries)} add"
                + (f" (not: {', '.join(bad)})" if bad else ""),
                "all enabled filters",
                not bad,
            )
        )
    else:
        rows.append(_row("Ablations", "not run", "all enabled filters", None))

    statuses = {r["status"] for r in rows}
    verdict = "fail" if "fail" in statuses else ("pending" if "pending" in statuses else "pass")
    report = {
        "verdict": verdict,
        "criteria": rows,
        "oos": st,
        "slip2": slip,
        "benchmark": bench,
        "monte_carlo": mc,
        "monte_carlo_delay1": mc_delay,
    }
    (wf_dir / "acceptance.json").write_text(json.dumps(report, indent=1, default=str))
    return report
