"""Phase two of a backtest: the portfolio loop over 1H closes.

For each 1H close ``t`` (across every symbol, in time order):

1. ``broker.process_hour``: the hour that just ended, minute by minute (fills
   of orders submitted at the previous close, stops, take-profits, funding).
2. Mark to market at ``t``; update the breakers.
3. Signals whose bar closed at ``t``: universe membership, quarantine and
   data-sanity checks, sizing, the liquidation rule and the portfolio checks;
   accepted ones fill at the next open.
4. Open positions: the exit rules (``core.exits.evaluate``) with this hour's
   ATR, momentum decay and CHoCH from the tape; stop moves apply now, exits
   fill at the next open.

A 4H or 1D bar is visible only at its close (the tape guarantees it), and a
signal at ``t`` can only fill at ``t``'s next open, so no step reads data from
after the moment it acts.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from zone_trend.backtest.broker_sim import Broker, PendingEntry
from zone_trend.backtest.market import Funding, MinuteSource
from zone_trend.backtest.tape import build_tapes
from zone_trend.config import Config
from zone_trend.core import exits
from zone_trend.core.indicators import pearson
from zone_trend.data.exchange import latest_filters
from zone_trend.data.store import DataPaths, load, read_snapshot
from zone_trend.risk.displacement import DisplacementTable
from zone_trend.risk.liquidation import stop_within_liq
from zone_trend.risk.portfolio import OpenExposure, PortfolioGuard
from zone_trend.risk.sizing import DEFAULT_FILTERS, Filters, size_order
from zone_trend.types import Signal

__all__ = ["RunResult", "RunSpec", "run"]

HOUR = 3_600_000
DAY = 24 * HOUR
TOP_SLIPPAGE = 0.0002
OTHER_SLIPPAGE = 0.0005
TOP_N = 5
BTC = "BTCUSDT"

APPROXIMATIONS = [
    "exchange filters (tick, step, minNotional) from the newest exchangeInfo snapshot, "
    "not as of each trade; delisted symbols use step 1e-8 and a $5 minimum",
    "leverage bracket: 1% maintenance margin rate for every symbol (pessimistic)",
    "mark-price minutes missing on Vision fall back to last-price minutes",
    "a stop and a take-profit in the same minute resolve as the stop",
    "market entries only (the limit-at-zone-edge option is not simulated)",
]


@dataclass
class RunSpec:
    start: str  # YYYY-MM
    end: str  # YYYY-MM, inclusive
    symbols: list[str] | None = None  # fixed list; None means the universe history
    universe: dict[str, list[str]] | None = None
    kind: str = "zone"  # "zone" | "ema"
    equity: float = 500.0
    slippage_mult: float = 1.0
    displacement: DisplacementTable | None = None
    workers: int = 4
    use_cache: bool = True

    def bounds(self) -> tuple[int, int]:
        start = pd.Timestamp(self.start + "-01", tz="UTC")
        end = pd.Timestamp(self.end + "-01", tz="UTC") + pd.offsets.MonthBegin()
        return int(start.value // 1_000_000), int(end.value // 1_000_000)

    def members(self, month: str) -> list[str]:
        if self.symbols is not None:
            return self.symbols
        return (self.universe or {}).get(month, [])


@dataclass
class RunResult:
    run_id: str
    trades: pd.DataFrame
    fills: pd.DataFrame
    equity: pd.DataFrame
    signals: pd.DataFrame
    meta: dict = field(default_factory=dict)


def _month(t: int) -> str:
    return pd.Timestamp(t + 1, unit="ms", tz="UTC").strftime("%Y-%m")


def _day(t: int) -> str:
    return pd.Timestamp(t, unit="ms", tz="UTC").strftime("%Y-%m-%d")


class _Correlation:
    """30-day correlation of daily log returns, from 1D closes before ``t``."""

    def __init__(self, paths: DataPaths, symbols: list[str], window_days: int):
        self.window = window_days
        self.data = {}
        for s in symbols:
            d1 = load(paths, "klines", s, "1d", columns=["close_time", "close"])
            self.data[s] = (
                d1["close_time"].to_numpy(np.int64),
                np.diff(np.log(d1["close"].to_numpy(float)), prepend=np.nan),
            )
        self._cache: dict[tuple[str, str, int], float] = {}

    def __call__(self, a: str, b: str, t: int) -> float:
        day = t // DAY
        key = (min(a, b), max(a, b), day)
        if key not in self._cache:
            ta, ra = self.data[a]
            tb, rb = self.data[b]
            sa = pd.Series(ra[ta < t][-self.window :], index=ta[ta < t][-self.window :])
            sb = pd.Series(rb[tb < t][-self.window :], index=tb[tb < t][-self.window :])
            joined = pd.concat([sa, sb], axis=1, join="inner").dropna()
            self._cache[key] = (
                pearson(joined.iloc[:, 0], joined.iloc[:, 1]) if len(joined) >= 10 else math.nan
            )
        return self._cache[key]


def _run_id(cfg: Config, spec: RunSpec, snapshot: str) -> str:
    blob = json.dumps(
        [
            cfg.model_dump(mode="json"),
            snapshot,
            spec.start,
            spec.end,
            spec.symbols,
            spec.universe,
            spec.kind,
            spec.equity,
            spec.slippage_mult,
            sorted(
                (f"{k[0]}|{k[1]}", v)
                for k, v in (spec.displacement or DisplacementTable()).k.items()
            ),
        ],
        sort_keys=True,
    ).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def run(paths: DataPaths, cfg: Config, spec: RunSpec) -> RunResult:
    snap = read_snapshot(paths)
    snapshot_id = snap["snapshot_id"]
    quarantine = snap.get("quarantine", {})
    flags = {s: r.get("flags", {}) for s, r in snap.get("report", {}).get("symbols", {}).items()}
    start_ms, end_ms = spec.bounds()

    months = pd.period_range(spec.start, spec.end, freq="M").strftime("%Y-%m")
    union = sorted({s for m in months for s in spec.members(m)})
    # The BTC gate needs BTC's 1D trend even when BTC is not traded.
    gate = spec.kind == "zone" and bool(paths.months("klines", BTC, "1h"))
    tape_symbols = sorted(set(union) | ({BTC} if gate else set()))
    tapes = build_tapes(
        paths, tape_symbols, cfg, snapshot_id, end_ms, spec.kind, spec.workers, spec.use_cache
    )
    btc = tapes.get(BTC) if gate else None

    info = latest_filters(paths)
    filters = {
        s: Filters(r["tick"], r["step"], r["min_qty"], r["min_notional"]) if r else DEFAULT_FILTERS
        for s in union
        for r in [info.get(s)]
    }
    corr = _Correlation(paths, union, cfg.filters.corr_window_days)
    minutes = MinuteSource(paths)
    funding = Funding(paths, union)
    guard = PortfolioGuard(cfg.portfolio, cfg.filters)
    broker = Broker(spec.equity, cfg.exchange.fee_taker, cfg.exchange.leverage)

    hours = np.unique(
        np.concatenate(
            [
                tp.close_time[(tp.close_time >= start_ms) & (tp.close_time < end_ms)]
                for tp in tapes.values()
            ]
        )
    )
    signal_rows: list[dict] = []
    equity_rows: list[dict] = []
    n_closed = 0

    def record(sig: Signal, stage: str, decision: str, reason: str = "", **extra) -> None:
        signal_rows.append(
            {
                "id": sig.id,
                "symbol": sig.symbol,
                "side": sig.side.value,
                "entry_type": sig.entry_type,
                "bar_time": sig.bar_time,
                "entry_ref": sig.entry_ref,
                "stop": sig.stop,
                "tp1": sig.tp1,
                "tp2": sig.tp2,
                "quality": sig.quality,
                "zone_id": sig.zone_id,
                "stage": stage,
                "decision": decision,
                "reason": reason,
                **extra,
            }
        )

    for t in hours:
        t = int(t)
        month = _month(t)
        members = spec.members(month)
        rank = {s: i for i, s in enumerate(members)}

        # 1. the hour that just ended
        broker.process_hour(t + 1 - HOUR, minutes, funding)
        for sig, why in broker.skipped:
            record(sig, "fill", "skipped", why)
        broker.skipped.clear()
        for tr in broker.closed[n_closed:]:
            guard.trade_closed(tr["exit_time"], tr["net"])
        n_closed = len(broker.closed)

        # 2. mark to market
        marks = {}
        for sym in broker.positions:
            tp = tapes[sym]
            i = tp.index(t)
            if i is not None:
                marks[sym] = float(tp.close[i])
        equity = broker.equity(marks)
        guard.mark(t, equity)
        equity_rows.append(
            {
                "time": t,
                "equity": equity,
                "cash": broker.cash,
                "positions": len(broker.positions),
                "margin": broker.margin(),
            }
        )

        # benchmark: exits on the opposite cross
        for sym, tp in tapes.items():
            side = tp.force_exit.get(t)
            if side is not None and sym in broker.positions:
                if broker.positions[sym].pos.side is side:
                    broker.request_exit(sym, "signal")

        # 3. signals
        candidates = [s for sym in union for s in tapes[sym].signals.get(t, [])]
        candidates.sort(key=lambda s: (-s.quality, s.symbol, s.id))
        for sig in candidates:
            sym = sig.symbol
            if sym not in rank:
                record(sig, "universe", "rejected", "not_in_universe")
                continue
            if _day(t) in quarantine.get(sym, {}):
                record(sig, "data", "rejected", "quarantine")
                continue
            if "mark_last" in flags.get(sym, {}).get(_day(t), []):
                record(sig, "data", "rejected", "data_sanity")
                continue
            if not stop_within_liq(sig.entry_ref, sig.stop, sig.side.sign, cfg.exchange.leverage):
                record(sig, "risk", "rejected", "liquidation_rule")
                continue
            available = equity - broker.margin()
            k_sym = 1.0
            if spec.displacement is not None and cfg.sizing.displacement.enabled:
                k_sym = spec.displacement.get(sym, sig.entry_type)
            mult = guard.size_mult()
            tp1_frac = cfg.exits.tp1_fraction if tapes[sym].managed else 1.0
            size = size_order(
                sig.entry_ref,
                sig.stop,
                equity,
                available,
                cfg.sizing,
                cfg.exchange,
                filters.get(sym, DEFAULT_FILTERS),
                tp1_frac,
                k_sym,
                mult,
            )
            if size is None:
                record(sig, "risk", "rejected", "min_notional")
                continue
            exposure = [
                OpenExposure(s, sp.pos.side, sp.notional_open) for s, sp in broker.positions.items()
            ] + [
                OpenExposure(s, pe.signal.side, pe.size.notional)
                for s, pe in broker.pending.items()
            ]
            corrs = {e.symbol: corr(sym, e.symbol, t) for e in exposure}
            reason = guard.check(
                sig,
                t,
                equity,
                size.notional,
                exposure,
                corrs,
                funding.last_8h(sym, t),
                btc.trend_at(t) if btc is not None else None,
            )
            if reason:
                record(sig, "risk", "rejected", reason)
                continue
            tick = filters.get(sym, DEFAULT_FILTERS).tick
            base = TOP_SLIPPAGE if rank[sym] < TOP_N else OTHER_SLIPPAGE
            slip = (base + tick / sig.entry_ref) * spec.slippage_mult
            broker.submit(PendingEntry(sig, size, slip, k_sym, mult, tapes[sym].managed))
            guard.trade_opened()
            record(
                sig,
                "risk",
                "accepted",
                notional=size.notional,
                single_tp=size.single_tp,
                k_sym=k_sym,
                size_mult=mult,
            )

        # 4. manage open positions
        for sym, sp in list(broker.positions.items()):
            p = sp.pos
            if p.opened_at > t or not sp.managed:
                continue
            p.bars_open += 1
            tp = tapes[sym]
            i = tp.index(t)
            if i is None or math.isnan(tp.atr_h1[i]):
                continue
            decay = bool(tp.decay_long[i] if p.side.sign > 0 else tp.decay_short[i])
            for act in exits.evaluate(
                p,
                float(tp.atr_h1[i]),
                decay=decay,
                choch_against=tp.choch_against(p.side, p.opened_at, t),
                fee_taker=cfg.exchange.fee_taker,
                cfg=cfg.exits,
            ):
                if isinstance(act, exits.MoveStop):
                    p.stop = act.price
                else:
                    broker.request_exit(sym, act.reason)

    # core-level rejections, for symbols in the universe at the time
    core_rej = [
        {
            "id": "",
            "symbol": r.symbol,
            "side": r.side.value,
            "entry_type": r.entry_type,
            "bar_time": r.bar_time,
            "zone_id": r.zone_id,
            "stage": "core",
            "decision": "rejected",
            "reason": r.reason,
            "detail": r.detail,
        }
        for sym in union
        for r in tapes[sym].rejected
        if start_ms <= r.bar_time < end_ms and sym in spec.members(_month(r.bar_time))
    ]

    run_id = _run_id(cfg, spec, snapshot_id)
    meta = {
        "run_id": run_id,
        "kind": spec.kind,
        "range": [spec.start, spec.end],
        "symbols": union,
        "snapshot_id": snapshot_id,
        "exchange_info": info.snapshot,
        "start_equity": spec.equity,
        "slippage_mult": spec.slippage_mult,
        "same_minute_conflicts": broker.conflicts,
        "mark_minutes_missing": minutes.mark_missing,
        "breaker_events": guard.events,
        "halted": guard.halted,
        "open_at_end": sorted(broker.positions),
        "approximations": APPROXIMATIONS,
    }
    return RunResult(
        run_id,
        pd.DataFrame(broker.closed),
        pd.DataFrame(broker.fills),
        pd.DataFrame(equity_rows),
        pd.DataFrame(signal_rows + core_rej),
        meta,
    )
