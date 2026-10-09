"""Walk-forward optimisation, out-of-sample stitching and the runs M4 needs.

Windows: 18 months in-sample, 6 months out-of-sample, stepping 6 months, from
``start`` until the out-of-sample window would reach the holdout (the last 9
months, never touched here).

Grid: the spec's "each parameter at default ±20% and ±40%", read as
one-at-a-time perturbations of ``PARAMS`` (a full factorial over nine
parameters would be 5^9 runs). Invalid variants (e.g. TP2 not beyond TP1)
are dropped.

Selection: each candidate runs on the three 6-month folds of the in-sample
window; its score is the median over folds of::

    min(PF, 3) * sqrt(trades) * max(0, 1 - maxDD / dd_halt_pct)

The best candidate is frozen and run out-of-sample, compounding from the
previous window's ending equity, with a displacement table built from its
in-sample trades. The same out-of-sample windows are also run with that
candidate at 2x slippage and with a one-hour entry delay, and with the EMA
benchmark, so every comparison covers identical months.

Robustness: the default config and each of its ±20% neighbours run once over
the whole out-of-sample span, with no optimisation.
"""

from __future__ import annotations

import json
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from zone_trend.backtest.engine import RunSpec, run
from zone_trend.backtest.metrics import max_drawdown, profit_factor, summary
from zone_trend.backtest.tape import build_tapes
from zone_trend.config import Config
from zone_trend.data.store import DataPaths, read_snapshot
from zone_trend.risk.displacement import DisplacementTable

__all__ = [
    "PARAMS",
    "Window",
    "candidates",
    "neighbours",
    "plan_windows",
    "score",
    "walk_forward",
]

PARAMS: list[tuple[str, str]] = [
    ("entries", "breakout_buffer_atr"),
    ("entries", "volume_mult"),
    ("entries", "retest_tolerance_atr"),
    ("exits", "stop_buffer_atr"),
    ("exits", "tp1_r"),
    ("exits", "tp2_r"),
    ("exits", "trail_atr"),
    ("structure", "decay_ratio"),
    ("swings", "k_atr"),
]
GRID_MULTS = (0.6, 0.8, 1.2, 1.4)
NEIGHBOUR_MULTS = (0.8, 1.2)
PF_CAP = 3.0
IS_MONTHS, OOS_MONTHS, STEP_MONTHS, HOLDOUT_MONTHS = 18, 6, 6, 9


def _shift(month: str, n: int) -> str:
    return str(pd.Period(month, freq="M") + n)


@dataclass(frozen=True)
class Window:
    is_start: str
    is_end: str  # inclusive
    oos_start: str
    oos_end: str

    def folds(self) -> list[tuple[str, str]]:
        size = IS_MONTHS // 3
        return [
            (_shift(self.is_start, i * size), _shift(self.is_start, (i + 1) * size - 1))
            for i in range(3)
        ]


def holdout_range(end: str) -> tuple[str, str]:
    return _shift(end, -(HOLDOUT_MONTHS - 1)), end


def plan_windows(start: str, end: str) -> list[Window]:
    """Windows from ``start`` whose out-of-sample part ends before the holdout."""
    last_oos = _shift(holdout_range(end)[0], -1)
    out = []
    s = start
    while _shift(s, IS_MONTHS + OOS_MONTHS - 1) <= last_oos:
        out.append(
            Window(
                s,
                _shift(s, IS_MONTHS - 1),
                _shift(s, IS_MONTHS),
                _shift(s, IS_MONTHS + OOS_MONTHS - 1),
            )
        )
        s = _shift(s, STEP_MONTHS)
    return out


def variant(cfg: Config, section: str, name: str, mult: float) -> Config | None:
    data = cfg.model_dump(mode="json")
    data[section][name] = round(data[section][name] * mult, 10)
    try:
        return Config.model_validate(data)
    except ValueError:
        return None


def candidates(cfg: Config) -> list[tuple[str, Config]]:
    out = [("default", cfg)]
    for section, name in PARAMS:
        for m in GRID_MULTS:
            v = variant(cfg, section, name, m)
            if v is not None:
                out.append((f"{section}.{name}x{m}", v))
    return out


def neighbours(cfg: Config) -> list[tuple[str, Config]]:
    out = []
    for section, name in PARAMS:
        for m in NEIGHBOUR_MULTS:
            v = variant(cfg, section, name, m)
            if v is not None:
                out.append((f"{section}.{name}x{m}", v))
    return out


def score(trades: pd.DataFrame, equity: pd.DataFrame, dd_halt: float) -> float:
    if trades.empty:
        return 0.0
    pf = profit_factor(trades["net"])
    pf = PF_CAP if math.isinf(pf) else min(pf, PF_CAP)
    dd = max_drawdown(equity)
    return pf * math.sqrt(len(trades)) * max(0.0, 1 - dd / dd_halt)


# -- jobs (module-level so they pickle into worker processes) ---------------------
@dataclass
class _Job:
    key: tuple
    root: str
    cfg: Config
    spec: RunSpec


def _run_job(job: _Job):
    res = run(DataPaths(Path(job.root)), job.cfg, job.spec)
    return job.key, res.trades, res.equity, res.meta


def _map(jobs: list[_Job], workers: int) -> dict[tuple, tuple]:
    out = {}
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        for key, trades, equity, meta in pool.map(_run_job, jobs, chunksize=1):
            out[key] = (trades, equity, meta)
    return out


@dataclass
class WFResult:
    wf_id: str
    windows: list[dict] = field(default_factory=list)
    oos: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = field(default_factory=dict)
    robustness: list[dict] = field(default_factory=list)


def _stitch(parts: list[tuple[pd.DataFrame, pd.DataFrame]]):
    trades = pd.concat([t for t, _ in parts if not t.empty], ignore_index=True) if parts else None
    equity = pd.concat([e for _, e in parts], ignore_index=True)
    return (trades if trades is not None else pd.DataFrame()), equity


def walk_forward(
    paths: DataPaths,
    cfg: Config,
    universe: dict[str, list[str]] | None,
    symbols: list[str] | None,
    start: str,
    end: str,
    out_dir: Path,
    equity: float = 500.0,
    workers: int = 4,
    log=print,
) -> WFResult:
    snapshot = read_snapshot(paths)["snapshot_id"]
    windows = plan_windows(start, end)
    if not windows:
        raise ValueError(f"no complete walk-forward window between {start} and {end}")
    tape_end = _shift(holdout_range(end)[0], -1)  # nothing from the holdout, ever
    cands = candidates(cfg)
    nbrs = neighbours(cfg)
    months = pd.period_range(start, tape_end, freq="M").strftime("%Y-%m")
    tape_syms = sorted(
        {s for m in months for s in (symbols or (universe or {}).get(m, []))} | {"BTCUSDT"}
    )
    tape_syms = [s for s in tape_syms if paths.months("klines", s, "1h")]

    log(f"# {len(windows)} windows, {len(cands)} candidates, {len(tape_syms)} symbols")
    for i, (label, c) in enumerate(cands + nbrs):
        build_tapes(paths, tape_syms, c, snapshot, _tape_end_ms(tape_end), "zone", workers)
        log(f"  tapes {i + 1}/{len(cands) + len(nbrs)} {label}")
    build_tapes(paths, tape_syms, cfg, snapshot, _tape_end_ms(tape_end), "ema", workers)

    def spec(s, e, **kw) -> RunSpec:
        return RunSpec(
            s,
            e,
            symbols=symbols,
            universe=universe,
            equity=kw.pop("equity", equity),
            workers=1,
            tape_end=tape_end,
            **kw,
        )

    # 1. in-sample folds for every candidate in every window
    jobs = [
        _Job((wi, ci, fi), str(paths.root), c, spec(fs, fe))
        for wi, w in enumerate(windows)
        for ci, (_, c) in enumerate(cands)
        for fi, (fs, fe) in enumerate(w.folds())
    ]
    log(f"# in-sample: {len(jobs)} runs")
    is_runs = _map(jobs, workers)

    result = WFResult(wf_id=out_dir.name)
    chosen: list[tuple[Window, int, DisplacementTable]] = []
    for wi, w in enumerate(windows):
        scores = []
        for ci, (label, _) in enumerate(cands):
            folds = [is_runs[(wi, ci, fi)] for fi in range(3)]
            s = statistics.median(score(t, e, cfg.portfolio.dd_halt_pct) for t, e, _ in folds)
            scores.append((s, -ci, label, ci))
        best = max(scores)
        ci = best[3]
        is_trades = pd.concat([is_runs[(wi, ci, fi)][0] for fi in range(3)], ignore_index=True)
        table = (
            DisplacementTable.from_trades(
                is_trades,
                _tape_end_ms(w.is_end),
                cfg.sizing.displacement.min_trades,
                tuple(cfg.sizing.displacement.clamp),
            )
            if not is_trades.empty
            else DisplacementTable()
        )
        chosen.append((w, ci, table))
        top = sorted(scores, reverse=True)[:5]
        result.windows.append(
            {
                "window": w.__dict__,
                "chosen": cands[ci][0],
                "score": best[0],
                "top5": [{"label": t[2], "score": t[0]} for t in top],
                "is_trades": len(is_trades),
                "k_sym": {f"{k[0]}|{k[1]}": v for k, v in table.k.items()},
            }
        )
        log(f"  {w.oos_start}..{w.oos_end}: {cands[ci][0]} (score {best[0]:.2f})")

    # 2. out-of-sample variants, compounding window to window (sequential by nature)
    variants = {
        "oos": {},
        "oos_slip2": {"slippage_mult": 2.0},
        "oos_delay1": {"entry_delay_hours": 1},
        "benchmark": {"kind": "ema"},
    }
    for name, extra in variants.items():
        eq = equity
        parts = []
        for w, ci, table in chosen:
            c = cfg if name == "benchmark" else cands[ci][1]
            disp = None if name == "benchmark" else table
            res = run(paths, c, spec(w.oos_start, w.oos_end, equity=eq, displacement=disp, **extra))
            parts.append((res.trades, res.equity))
            if len(res.equity):
                eq = float(res.equity["equity"].iloc[-1])
        result.oos[name] = _stitch(parts)
        t, e = result.oos[name]
        log(f"  {name}: {len(t)} trades, end equity {e['equity'].iloc[-1]:.2f}")

    # 3. robustness: default and its ±20% neighbours over the whole OOS span
    span = (windows[0].oos_start, windows[-1].oos_end)
    jobs = [
        _Job(("nbr", i), str(paths.root), c, spec(*span))
        for i, (_, c) in enumerate([("default", cfg), *nbrs])
    ]
    nb = _map(jobs, workers)
    for i, (label, _) in enumerate([("default", cfg), *nbrs]):
        t, e, _ = nb[("nbr", i)]
        st = summary(t, e)
        result.robustness.append(
            {
                "label": label,
                "trades": st["trades"],
                "profit_factor": st["profit_factor"],
                "expectancy_r": st["expectancy_r"],
                "max_drawdown": st["max_drawdown"],
            }
        )
    _write(result, out_dir, cfg, start, end, symbols, universe is not None)
    return result


def _tape_end_ms(month: str) -> int:
    return int((pd.Period(month, freq="M") + 1).start_time.tz_localize("UTC").value // 1_000_000)


def _write(res: WFResult, out: Path, cfg: Config, start, end, symbols, used_universe) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, (t, e) in res.oos.items():
        t.to_parquet(out / f"{name}_trades.parquet", index=False)
        e.to_parquet(out / f"{name}_equity.parquet", index=False)
    meta = {
        "wf_id": res.wf_id,
        "range": [start, end],
        "holdout": list(holdout_range(end)),
        "symbols": symbols,
        "universe_history": used_universe,
        "windows": res.windows,
        "robustness": res.robustness,
        "config": cfg.model_dump(mode="json"),
    }
    (out / "wf.json").write_text(json.dumps(meta, indent=1, default=str))
