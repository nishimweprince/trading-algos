"""Unattended-operation duties of the runner: heartbeat, summaries, gates.

- ``heartbeat``: every minute the runner writes ``bot_state.heartbeat`` and a
  metrics snapshot (equity, drawdown, positions, API weight). The status app
  turns them into ``/health`` (503 when the heartbeat is older than 15 min, for
  an external uptime monitor: a dead bot cannot alert on itself) and
  ``/metrics``.
- ``daily_summary``: at the 00:00 UTC close; equity, day PnL, open risk,
  drawdown, to Telegram and email.
- ``weekly_report``: Monday 00:00 UTC; live vs backtest per symbol (trades,
  expectancy), entry slippage against the model, and the displacement refresh
  (``k_sym`` from backtest trades, blended 70/30 with live once 30 live trades
  exist), written to ``displacement_stats``.
- ``stage_gates``: the spec's rollout gates, evaluated daily. With
  ``ZT_STAGE=auto`` the runner advances micro -> half -> full by itself when a
  gate passes; a stage that misses its gate stays and alerts. Shadow -> micro is
  never automatic: it means starting the live process with the acknowledgement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import delete, insert, select

from zone_trend.backtest.metrics import profit_factor
from zone_trend.persistence import models as m
from zone_trend.persistence.repo import Repo
from zone_trend.risk.decide import slippage
from zone_trend.risk.displacement import DisplacementTable

__all__ = [
    "HEARTBEAT_STALE_MS",
    "GateResult",
    "daily_summary",
    "refresh_displacement",
    "stage_gates",
    "weekly_report",
    "write_metrics",
]

DAY = 86_400_000
HEARTBEAT_STALE_MS = 15 * 60_000


def _closed(repo: Repo, since: int = 0) -> pd.DataFrame:
    rows = [
        p for p in repo.closed_positions() if p["entry_price"] and (p["closed_at"] or 0) >= since
    ]
    return pd.DataFrame(rows)


def _r(df: pd.DataFrame) -> pd.Series:
    risk = df["qty"] * (df["entry_price"] - df["stop_initial"]).abs()
    return df["pnl"] / risk.replace(0, np.nan)


def daily_summary(repo: Repo, t: int, equity: float, peak: float, open_risk: float) -> list[str]:
    day = _closed(repo, t + 1 - DAY)
    pnl = float(day["pnl"].sum()) if len(day) else 0.0
    dd = 1 - equity / peak if peak > 0 else 0.0
    return [
        f"equity {equity:,.2f} (peak {peak:,.2f}, drawdown {dd:.1%})",
        f"closed today: {len(day)} trades, PnL {pnl:+,.2f}",
        f"open positions: {len(repo.open_positions())}, open risk {open_risk:,.2f}",
    ]


def _backtest_trades(wf_dir: Path | None) -> pd.DataFrame:
    if wf_dir is None or not (wf_dir / "oos_trades.parquet").exists():
        return pd.DataFrame()
    return pd.read_parquet(wf_dir / "oos_trades.parquet")


def refresh_displacement(repo: Repo, wf_dir: Path | None, cfg, now: int) -> DisplacementTable:
    """k_sym from backtest OOS trades, blended with live trades once there are enough."""
    disp = cfg.sizing.displacement
    bt = _backtest_trades(wf_dir)
    table = (
        DisplacementTable.from_trades(bt, now, disp.min_trades, tuple(disp.clamp))
        if len(bt)
        else DisplacementTable()
    )
    live = _closed(repo)
    if len(live) >= disp.min_trades and len(bt):
        live = live.assign(
            mfe_r=(live["best"] - live["entry_price"]).abs()
            / (live["entry_price"] - live["stop_initial"]).abs(),
            mae_r=(live["worst"] - live["entry_price"]).abs()
            / (live["entry_price"] - live["stop_initial"]).abs(),
            exit_time=live["closed_at"],
        )
        lt = DisplacementTable.from_trades(live, now, disp.min_trades, tuple(disp.clamp))
        w = disp.live_weight
        for key in set(table.k) | set(lt.k):
            table.k[key] = (1 - w) * table.k.get(key, 1.0) + w * lt.k.get(key, 1.0)
    with repo.engine.begin() as conn:
        conn.execute(delete(m.displacement_stats))
        for (sym, etype), stats in table.stats.items():
            conn.execute(
                insert(m.displacement_stats).values(
                    symbol=sym,
                    entry_type=etype,
                    source="blended",
                    n=stats["n"],
                    mfe_p50_r=stats["mfe_p50_r"],
                    mfe_p75_r=stats["mfe_p75_r"],
                    mae_p50_r=stats["mae_p50_r"],
                    k_sym=table.get(sym, etype),
                    updated_at=now,
                )
            )
    repo.set_state("k_sym_cache", {f"{k[0]}|{k[1]}": v for k, v in table.k.items()})
    return table


def _slippage_stats(repo: Repo, ranks: dict[str, int]) -> tuple[float, float]:
    """Mean live entry slippage vs the model's, as fractions of price."""
    sigs = {s["id"]: s for s in repo.signals_between(0, 1 << 62)}
    live, model = [], []
    for p in repo.closed_positions() + repo.open_positions():
        s = sigs.get(p["signal_id"])
        if not s or not p["entry_price"] or not s["entry_ref"] or math.isnan(s["entry_ref"]):
            continue
        sign = 1 if p["side"] == "long" else -1
        live.append(sign * (p["entry_price"] - s["entry_ref"]) / s["entry_ref"])
        model.append(slippage(ranks.get(p["symbol"], 99), 0.0, s["entry_ref"]))
    if not live:
        return math.nan, math.nan
    return float(np.mean(live)), float(np.mean(model))


def weekly_report(
    repo: Repo, wf_dir: Path | None, cfg, ranks: dict[str, int], now: int
) -> list[str]:
    lines = []
    live = _closed(repo)
    bt = _backtest_trades(wf_dir)
    symbols = sorted(set(live.get("symbol", pd.Series(dtype=str))) | set(bt.get("symbol", [])))
    for sym in symbols:
        lv = live[live["symbol"] == sym] if len(live) else live
        b = bt[bt["symbol"] == sym] if len(bt) else bt
        le = _r(lv).mean() if len(lv) else math.nan
        be = b["r_mult"].mean() if len(b) else math.nan
        lines.append(f"{sym}: live {len(lv)} trades {le:+.2f}R | backtest {len(b)} {be:+.2f}R")
    s_live, s_model = _slippage_stats(repo, ranks)
    if not math.isnan(s_live):
        lines.append(f"entry slippage: live {s_live:.4%} vs model {s_model:.4%}")
    table = refresh_displacement(repo, wf_dir, cfg, now)
    if table.k:
        top = ", ".join(f"{k[0]} {v:.2f}" for k, v in sorted(table.k.items())[:10])
        lines.append(f"k_sym refreshed: {top}")
    return lines or ["no trades yet"]


@dataclass(frozen=True)
class GateResult:
    stage: str
    next_stage: str | None
    passed: bool
    detail: list[str]


def _pf_interval(bt: pd.DataFrame, n: int, paths: int = 2000) -> tuple[float, float]:
    """5th-95th percentile PF of ``n`` backtest trades drawn with replacement."""
    if len(bt) < 2 or n < 2:
        return math.nan, math.nan
    rng = np.random.default_rng(0)
    net = bt["net"].to_numpy()
    pfs = []
    for _ in range(paths):
        sample = pd.Series(net[rng.integers(0, len(net), n)])
        pf = profit_factor(sample)
        pfs.append(min(pf, 10.0) if not math.isnan(pf) else 0.0)
    return float(np.percentile(pfs, 5)), float(np.percentile(pfs, 95))


def stage_gates(repo: Repo, wf_dir: Path | None, ranks: dict[str, int], now: int) -> GateResult:
    state = repo.get_state("stage", {"name": "micro", "since": now})
    stage, since = state["name"], int(state["since"])
    trades = _closed(repo, since)
    detail: list[str] = []
    if stage == "micro":
        weeks = (now - since) / (7 * DAY)
        unprotected = [
            e
            for e in _events(repo, since)
            if e["level"] == "critical" and "unprotected" in e["payload"]
        ]
        s_live, s_model = _slippage_stats(repo, ranks)
        slip_ok = math.isnan(s_live) or s_live <= 1.5 * s_model
        enough = len(trades) >= 20 or weeks >= 4
        detail = [
            f"trades {len(trades)}/20 or weeks {weeks:.1f}/4",
            f"unprotected incidents {len(unprotected)} (need 0)",
            f"slippage {s_live:.4%} vs 1.5x model {1.5 * s_model:.4%}",
        ]
        return GateResult(stage, "half", enough and not unprotected and slip_ok, detail)
    if stage == "half":
        lo, hi = _pf_interval(_backtest_trades(wf_dir), len(trades))
        pf = profit_factor(trades["pnl"]) if len(trades) else math.nan
        inside = not math.isnan(lo) and lo <= pf <= hi
        detail = [
            f"trades {len(trades)}/40",
            f"live PF {pf:.2f} in backtest 90% [{lo:.2f}, {hi:.2f}]",
        ]
        return GateResult(stage, "full", len(trades) >= 40 and inside, detail)
    return GateResult(stage, None, False, ["full size: monthly live-vs-backtest review"])


def _events(repo: Repo, since: int) -> list[dict]:
    with repo.engine.begin() as conn:
        rows = conn.execute(select(m.events).where(m.events.c.ts >= since)).mappings().all()
    return [dict(r) for r in rows]


def write_metrics(repo: Repo, now: int, **values: float) -> None:
    repo.set_state("heartbeat", now)
    repo.set_state("metrics", {"ts": now, **values})
