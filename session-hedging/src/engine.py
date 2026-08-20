"""Closed-bar session-open hedge engine. Shared by backtest and paper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from anchors import (
    SessionAnchor,
    anchor_from_window,
    drift_minutes,
    entry_time,
    is_anchor_weekday,
    percentile_50,
    session_anchor_ts,
    session_day_key,
)
from costs import (
    CostBreakdown,
    CostSchedule,
    breakeven_cost_per_side,
    headroom_ratio,
    leg_cost,
    schedule_for,
)
from fills import TickPathUnavailable, after_lock_same_bar, m1_covering
from metrics import classify_pair, headline
from models import (
    BacktestReport,
    Candle,
    ClosedLeg,
    CostModel,
    EngineEvent,
    EngineParams,
    IntrabarMode,
    OpenPairView,
    OutcomeMix,
    SessionAnchorStats,
    Stats,
    StopMode,
    Timeframe,
    TradePairLeg,
    TradePairResult,
)
from sessions import SessionWindow
from units import cash, pips_raw, pips_weighted, r_multiple
from validation import GAP, validate_bar


@dataclass
class PendingSignal:
    session: str
    range_price: float
    bullish: bool
    signal_ts: datetime
    entry_time: datetime
    anchor_drift_minutes: float = 0.0


@dataclass
class OrbCollector:
    session: str
    anchor_ts: datetime
    first_open: datetime | None = None
    first_open_px: float | None = None
    high: float | None = None
    low: float | None = None
    last_close: float | None = None
    skipped: bool = False


@dataclass
class Pair:
    id: str
    session: str
    entry: float
    sl_dist: float
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    qty: float = 1.0
    primary_side: Literal["long", "short"] | None = None
    long_open: bool = True
    short_open: bool = True
    locked: bool = False
    entry_ts: datetime = field(default_factory=datetime.now)
    long_entry: float | None = None
    short_entry: float | None = None
    long_mae_pips: float = 0.0
    long_mfe_pips: float = 0.0
    short_mae_pips: float = 0.0
    short_mfe_pips: float = 0.0
    first_close_ts: datetime | None = None
    same_bar_resolved: bool = False


@dataclass
class CostAccounting:
    gross_realized_pips: float = 0.0
    realized_cost_pips: float = 0.0
    gross_unrealized_pips: float = 0.0
    unrealized_cost_pips: float = 0.0
    gross_realized_r: float = 0.0
    realized_cost_r: float = 0.0
    gross_unrealized_r: float = 0.0
    unrealized_cost_r: float = 0.0
    execution_cost_pips: float = 0.0
    financing_cost_pips: float = 0.0
    spread_cost_pips: float = 0.0
    realized_spread_cost_pips: float = 0.0
    transaction_sides: int = 0
    completed_transaction_sides: int = 0
    side_equivalents: float = 0.0
    completed_side_equivalents: float = 0.0


def bar_open(bar: Candle, timeframe_minutes: int) -> datetime:
    return bar.ts - timedelta(minutes=timeframe_minutes)


def _fill_stop(open_px: float, level: float, going_down: bool) -> float:
    if going_down:
        return open_px if open_px <= level else level
    return open_px if open_px >= level else level


def _fill_limit(open_px: float, level: float, is_long_tp: bool) -> float:
    if is_long_tp:
        return open_px if open_px >= level else level
    return open_px if open_px <= level else level


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


class ClosedBarEngine:
    """One step per closed bar of the configured timeframe. Fill is time-based."""

    def __init__(
        self,
        windows: list[SessionWindow],
        params: EngineParams,
        anchors: list[SessionAnchor] | None = None,
        m1_bars: list[Candle] | None = None,
    ) -> None:
        if params.intrabar_mode is IntrabarMode.TICK:
            raise TickPathUnavailable("INTRABAR_MODE=tick requires a tick source (not implemented)")
        self.windows = windows
        self.params = params
        self.m1_bars = m1_bars or []
        resolved_anchors = anchors
        if resolved_anchors is None:
            resolved_anchors = [anchor_from_window(window) for window in windows]
        self.anchors_by_name: dict[str, SessionAnchor] = {
            anchor.name: anchor for anchor in resolved_anchors
        }
        for window in windows:
            if window.name not in self.anchors_by_name:
                self.anchors_by_name[window.name] = anchor_from_window(window)
        self.pairs: list[Pair] = []
        self.pending: dict[str, PendingSignal] = {}
        self.orb: dict[str, OrbCollector] = {}
        self._done: set[str] = set()
        self.anchor_drifts: dict[str, list[float]] = {window.name: [] for window in windows}
        self.anchor_skips: dict[str, int] = {window.name: 0 for window in windows}
        self.prev_in_session: dict[str, bool] = {window.name: False for window in windows}
        self.stats = Stats()
        self.trades: list[ClosedLeg] = []
        self.events: list[EngineEvent] = []
        self.last_bar: Candle | None = None
        self.mintick = params.pip_size / 10.0
        self.be_eps = max(2 * self.mintick, 0.05 * params.pip_size)
        self.lock_dist = params.lock_pips * params.pip_size
        self.equity_peak_pips = 0.0
        self.max_drawdown_pips = 0.0
        self.equity_peak_r = 0.0
        self.max_drawdown_r = 0.0
        self.net_equity_peak_pips = 0.0
        self.net_max_drawdown_pips = 0.0
        self.net_equity_peak_r = 0.0
        self.net_max_drawdown_r = 0.0
        self._concurrent_samples: list[int] = []

    def observe(self, bar: Candle) -> None:
        """Record session membership without trading. Used to warm paper on first tick."""
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for window in self.windows:
            self.prev_in_session[window.name] = window.contains(open_ts)
            anchor = self.anchors_by_name.get(window.name)
            if anchor is None or not is_anchor_weekday(anchor, open_ts):
                continue
            anchor_ts = session_anchor_ts(anchor, open_ts)
            if open_ts >= anchor_ts:
                self._done.add(session_day_key(window.name, anchor_ts))
                self.orb.pop(window.name, None)
        self.last_bar = bar

    def _mark_elapsed_sessions(self, bar: Candle) -> None:
        """Mark session-days whose ORB window is already over. Backtest warmup."""
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for window in self.windows:
            anchor = self.anchors_by_name.get(window.name)
            if anchor is None or not is_anchor_weekday(anchor, open_ts):
                continue
            anchor_ts = session_anchor_ts(anchor, open_ts)
            orb_end = anchor_ts + timedelta(minutes=self.params.orb_minutes)
            if open_ts >= orb_end:
                self._done.add(session_day_key(window.name, anchor_ts))
                self.orb.pop(window.name, None)

    def step(self, bar: Candle) -> list[EngineEvent]:
        started = len(self.events)
        rejection = validate_bar(bar, self.last_bar, self.params.timeframe_minutes)
        if rejection is not None:
            self.events.append(
                EngineEvent(
                    kind="bar_skipped_invalid",
                    session="validation",
                    ts=bar.ts,
                    detail={"reason": rejection.reason, **rejection.detail},
                )
            )
            if rejection.reason != GAP:
                return self.events[started:]
        self._fill_pending(bar)
        self._record_excursions(bar)
        self._manage_pairs(bar)
        self._arm_signals(bar)
        self.last_bar = bar
        open_count = sum(1 for pair in self.pairs if pair.long_open or pair.short_open)
        self._concurrent_samples.append(open_count)
        self._record_equity(bar.close, bar)
        return self.events[started:]

    def run(self, candles: list[Candle]) -> None:
        if candles:
            self._mark_elapsed_sessions(candles[0])
        for bar in candles:
            self.step(bar)

    def report(
        self, symbol: str, timeframe: Timeframe, source: Literal["local", "ctrader"]
    ) -> BacktestReport:
        last_close = self.last_bar.close if self.last_bar is not None else 0.0
        unrealized = 0.0
        for pair in self.pairs:
            if pair.long_open:
                unrealized += self._pnl(True, self._leg_entry(pair, True), last_close)
            if pair.short_open:
                unrealized += self._pnl(False, self._leg_entry(pair, False), last_close)
        unrealized_pips = self._unrealized_pips(last_close)
        realized_r = self._realized_r()
        unrealized_r = self._unrealized_r(last_close)
        equity_pips = self._weighted_pips(self.stats.realized_pips + unrealized_pips)
        accounting = self._cost_accounting(
            last_close, self.last_bar.ts if self.last_bar is not None else None
        )
        gross_equity_pips = (
            accounting.gross_realized_pips + accounting.gross_unrealized_pips
        )
        equity_cost_pips = accounting.realized_cost_pips + accounting.unrealized_cost_pips
        gross_equity_r = accounting.gross_realized_r + accounting.gross_unrealized_r
        equity_cost_r = accounting.realized_cost_r + accounting.unrealized_cost_r
        breakeven = (
            breakeven_cost_per_side(
                accounting.gross_realized_pips, accounting.completed_side_equivalents
            )
            if self.params.breakeven_cost_report
            else None
        )
        configured_spread = (
            accounting.realized_spread_cost_pips / accounting.completed_side_equivalents
            if accounting.completed_side_equivalents > 0
            else (
                0.0
                if self.params.cost_model is CostModel.NONE
                else self.params.spread_pips_per_side
            )
        )
        configured_execution = (
            accounting.execution_cost_pips / accounting.side_equivalents
            if accounting.side_equivalents > 0
            else (
                0.0
                if self.params.cost_model is CostModel.NONE
                else self._base_cost_schedule().execution_pips_per_side
            )
        )
        realized_dollars = self._pips_to_dollars(self.stats.realized_pips)
        unrealized_dollars = self._pips_to_dollars(unrealized_pips)
        max_drawdown_dollars = self._pips_to_dollars(self.max_drawdown_pips)
        open_pairs = sum(1 for pair in self.pairs if pair.long_open or pair.short_open)
        metrics = self._headline_metrics()
        return BacktestReport(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            bar_count=0,
            performance_unit=self.params.performance_unit,
            orb_minutes=self.params.orb_minutes,
            entry_delay_minutes=self.params.entry_delay_minutes,
            anchor_tolerance_minutes=self.params.anchor_tolerance_minutes,
            stop_mode=self.params.stop_mode,
            fixed_stop_pips=self.params.fixed_stop_pips,
            realized=self.stats.realized,
            unrealized=unrealized,
            equity=equity_pips,
            realized_pips=self.stats.realized_pips,
            unrealized_pips=unrealized_pips,
            realized_r=realized_r,
            unrealized_r=unrealized_r,
            equity_pips=equity_pips,
            max_drawdown_pips=self.max_drawdown_pips,
            max_drawdown_r=self.max_drawdown_r,
            gross_max_drawdown_pips=self.max_drawdown_pips,
            net_max_drawdown_pips=self.net_max_drawdown_pips,
            gross_max_drawdown_r=self.max_drawdown_r,
            net_max_drawdown_r=self.net_max_drawdown_r,
            gross_realized_pips=accounting.gross_realized_pips,
            realized_cost_pips=accounting.realized_cost_pips,
            net_realized_pips=(
                accounting.gross_realized_pips - accounting.realized_cost_pips
            ),
            gross_unrealized_pips=accounting.gross_unrealized_pips,
            unrealized_cost_pips=accounting.unrealized_cost_pips,
            net_unrealized_pips=(
                accounting.gross_unrealized_pips - accounting.unrealized_cost_pips
            ),
            gross_equity_pips=gross_equity_pips,
            equity_cost_pips=equity_cost_pips,
            net_equity_pips=gross_equity_pips - equity_cost_pips,
            gross_realized_r=accounting.gross_realized_r,
            realized_cost_r=accounting.realized_cost_r,
            net_realized_r=accounting.gross_realized_r - accounting.realized_cost_r,
            gross_unrealized_r=accounting.gross_unrealized_r,
            unrealized_cost_r=accounting.unrealized_cost_r,
            net_unrealized_r=(
                accounting.gross_unrealized_r - accounting.unrealized_cost_r
            ),
            gross_equity_r=gross_equity_r,
            equity_cost_r=equity_cost_r,
            net_equity_r=gross_equity_r - equity_cost_r,
            execution_cost_pips=accounting.execution_cost_pips,
            financing_cost_pips=accounting.financing_cost_pips,
            transaction_sides=accounting.transaction_sides,
            completed_transaction_sides=accounting.completed_transaction_sides,
            cost_side_equivalents=accounting.side_equivalents,
            completed_cost_side_equivalents=accounting.completed_side_equivalents,
            breakeven_pips_per_side=breakeven,
            configured_spread_pips_per_side=configured_spread,
            configured_execution_cost_pips_per_side=configured_execution,
            cost_headroom_ratio=headroom_ratio(breakeven, configured_spread),
            realized_dollars=realized_dollars,
            unrealized_dollars=unrealized_dollars,
            equity_dollars=(
                self.params.initial_capital + realized_dollars + unrealized_dollars
                if realized_dollars is not None and unrealized_dollars is not None
                else None
            ),
            max_drawdown_dollars=max_drawdown_dollars,
            long_wins=self.stats.long_wins,
            long_be=self.stats.long_be,
            long_loss=self.stats.long_loss,
            short_wins=self.stats.short_wins,
            short_be=self.stats.short_be,
            short_loss=self.stats.short_loss,
            locks=self.stats.locks,
            open_pairs=open_pairs,
            session_anchor_stats=self._session_anchor_stats(),
            same_bar_resolution_rate=self._same_bar_resolution_rate(),
            same_bar_r=self._same_bar_r(),
            survivor_tp_rate=metrics.survivor_tp_rate,
            mean_loss_r=metrics.mean_loss_r,
            breakeven_tp_rate_required=metrics.breakeven_tp_rate_required,
            tp_rate_margin_pp=metrics.tp_rate_margin_pp,
            tp_rate_margin_pp_ci_low=metrics.tp_rate_margin_pp_ci_low,
            tp_rate_margin_pp_ci_high=metrics.tp_rate_margin_pp_ci_high,
            outcome_mix=OutcomeMix(
                tp=metrics.outcome_mix.tp,
                lock=metrics.outcome_mix.lock,
                breakeven=metrics.outcome_mix.breakeven,
                whipsaw=metrics.outcome_mix.whipsaw,
            ),
            max_concurrent_structures=metrics.max_concurrent_structures,
            median_concurrent=metrics.median_concurrent,
            trades=list(self.trades),
            trade_pairs=self._trade_pair_results(last_close),
            events=list(self.events),
        )

    def open_pair_views(self) -> list[OpenPairView]:
        views: list[OpenPairView] = []
        for pair in self.pairs:
            if not pair.long_open and not pair.short_open:
                continue
            views.append(
                OpenPairView(
                    id=pair.id,
                    session=pair.session,
                    entry=pair.entry,
                    sl_dist=pair.sl_dist,
                    long_open=pair.long_open,
                    short_open=pair.short_open,
                    locked=pair.locked,
                    long_sl=pair.long_sl,
                    long_tp=pair.long_tp,
                    short_sl=pair.short_sl,
                    short_tp=pair.short_tp,
                    entry_ts=pair.entry_ts,
                )
            )
        return views

    def snapshot(self) -> dict[str, object]:
        return {
            "prev_in_session": dict(self.prev_in_session),
            "done": sorted(self._done),
            "anchor_drifts": {name: list(values) for name, values in self.anchor_drifts.items()},
            "anchor_skips": dict(self.anchor_skips),
            "orb": {
                name: {
                    "session": collector.session,
                    "anchor_ts": collector.anchor_ts.isoformat(),
                    "first_open": (
                        collector.first_open.isoformat() if collector.first_open else None
                    ),
                    "first_open_px": collector.first_open_px,
                    "high": collector.high,
                    "low": collector.low,
                    "last_close": collector.last_close,
                    "skipped": collector.skipped,
                }
                for name, collector in self.orb.items()
            },
            "pending": {
                name: {
                    "session": signal.session,
                    "range_price": signal.range_price,
                    "bullish": signal.bullish,
                    "signal_ts": signal.signal_ts.isoformat(),
                    "entry_time": signal.entry_time.isoformat(),
                    "anchor_drift_minutes": signal.anchor_drift_minutes,
                }
                for name, signal in self.pending.items()
            },
            "pairs": [
                asdict(pair)
                | {
                    "entry_ts": pair.entry_ts.isoformat(),
                    "first_close_ts": (
                        pair.first_close_ts.isoformat() if pair.first_close_ts else None
                    ),
                }
                for pair in self.pairs
            ],
            "stats": self.stats.model_dump(),
            "trades": [leg.model_dump(mode="json") for leg in self.trades],
            "equity_peak_pips": self.equity_peak_pips,
            "max_drawdown_pips": self.max_drawdown_pips,
            "equity_peak_r": self.equity_peak_r,
            "max_drawdown_r": self.max_drawdown_r,
            "net_equity_peak_pips": self.net_equity_peak_pips,
            "net_max_drawdown_pips": self.net_max_drawdown_pips,
            "net_equity_peak_r": self.net_equity_peak_r,
            "net_max_drawdown_r": self.net_max_drawdown_r,
            "concurrent_samples": list(self._concurrent_samples),
        }

    def restore(self, payload: dict[str, object]) -> None:
        prev = payload.get("prev_in_session")
        if isinstance(prev, dict):
            self.prev_in_session = {str(k): bool(v) for k, v in prev.items()}
        done_raw = payload.get("done")
        self._done = set()
        if isinstance(done_raw, list):
            self._done = {str(item) for item in done_raw}
        drifts_raw = payload.get("anchor_drifts")
        if isinstance(drifts_raw, dict):
            self.anchor_drifts = {
                str(name): [float(v) for v in values] if isinstance(values, list) else []
                for name, values in drifts_raw.items()
            }
        skips_raw = payload.get("anchor_skips")
        if isinstance(skips_raw, dict):
            self.anchor_skips = {str(k): int(v) for k, v in skips_raw.items()}
        self.orb = {}
        orb_raw = payload.get("orb")
        if isinstance(orb_raw, dict):
            for name, raw in orb_raw.items():
                if not isinstance(raw, dict):
                    continue
                first_open = raw.get("first_open")
                self.orb[str(name)] = OrbCollector(
                    session=str(raw.get("session", name)),
                    anchor_ts=datetime.fromisoformat(str(raw["anchor_ts"])),
                    first_open=datetime.fromisoformat(str(first_open)) if first_open else None,
                    first_open_px=_optional_float(raw.get("first_open_px")),
                    high=_optional_float(raw.get("high")),
                    low=_optional_float(raw.get("low")),
                    last_close=_optional_float(raw.get("last_close")),
                    skipped=bool(raw.get("skipped", False)),
                )
        pending_raw = payload.get("pending")
        self.pending = {}
        if isinstance(pending_raw, dict):
            for name, raw in pending_raw.items():
                if not isinstance(raw, dict):
                    continue
                signal_ts = datetime.fromisoformat(str(raw["signal_ts"]))
                entry_raw = raw.get("entry_time")
                self.pending[str(name)] = PendingSignal(
                    session=str(raw["session"]),
                    range_price=float(raw["range_price"]),
                    bullish=bool(raw["bullish"]),
                    signal_ts=signal_ts,
                    entry_time=(
                        datetime.fromisoformat(str(entry_raw))
                        if entry_raw is not None
                        else signal_ts
                    ),
                    anchor_drift_minutes=float(raw.get("anchor_drift_minutes", 0.0)),
                )
        pairs_raw = payload.get("pairs")
        self.pairs = []
        if isinstance(pairs_raw, list):
            for raw in pairs_raw:
                if not isinstance(raw, dict):
                    continue
                self.pairs.append(
                    Pair(
                        id=str(raw["id"]),
                        session=str(raw["session"]),
                        entry=float(raw["entry"]),
                        sl_dist=float(raw["sl_dist"]),
                        long_sl=float(raw["long_sl"]),
                        long_tp=float(raw["long_tp"]),
                        short_sl=float(raw["short_sl"]),
                        short_tp=float(raw["short_tp"]),
                        qty=float(raw.get("qty", self.params.qty)),
                        primary_side=(
                            str(raw["primary_side"])
                            if raw.get("primary_side") in {"long", "short"}
                            else None
                        ),
                        long_open=bool(raw["long_open"]),
                        short_open=bool(raw["short_open"]),
                        locked=bool(raw["locked"]),
                        entry_ts=datetime.fromisoformat(str(raw["entry_ts"])),
                        long_entry=_optional_float(raw.get("long_entry")),
                        short_entry=_optional_float(raw.get("short_entry")),
                        long_mae_pips=float(raw.get("long_mae_pips", 0.0)),
                        long_mfe_pips=float(raw.get("long_mfe_pips", 0.0)),
                        short_mae_pips=float(raw.get("short_mae_pips", 0.0)),
                        short_mfe_pips=float(raw.get("short_mfe_pips", 0.0)),
                        first_close_ts=(
                            datetime.fromisoformat(str(raw["first_close_ts"]))
                            if raw.get("first_close_ts")
                            else None
                        ),
                        same_bar_resolved=bool(raw.get("same_bar_resolved", False)),
                    )
                )
        stats_raw = payload.get("stats")
        if isinstance(stats_raw, dict):
            self.stats = Stats.model_validate(stats_raw)
        trades_raw = payload.get("trades")
        self.trades = []
        if isinstance(trades_raw, list):
            self.trades = [ClosedLeg.model_validate(item) for item in trades_raw]
        if isinstance(stats_raw, dict) and "realized_pips" not in stats_raw:
            self.stats.realized_pips = sum(self._closed_leg_pips(leg) for leg in self.trades)
        peak = payload.get("equity_peak_pips")
        if peak is not None:
            self.equity_peak_pips = float(peak)
        dd = payload.get("max_drawdown_pips")
        if dd is not None:
            self.max_drawdown_pips = float(dd)
        peak_r = payload.get("equity_peak_r")
        if peak_r is not None:
            self.equity_peak_r = float(peak_r)
        dd_r = payload.get("max_drawdown_r")
        if dd_r is not None:
            self.max_drawdown_r = float(dd_r)
        net_peak = payload.get("net_equity_peak_pips")
        if net_peak is not None:
            self.net_equity_peak_pips = float(net_peak)
        net_dd = payload.get("net_max_drawdown_pips")
        if net_dd is not None:
            self.net_max_drawdown_pips = float(net_dd)
        net_peak_r = payload.get("net_equity_peak_r")
        if net_peak_r is not None:
            self.net_equity_peak_r = float(net_peak_r)
        net_dd_r = payload.get("net_max_drawdown_r")
        if net_dd_r is not None:
            self.net_max_drawdown_r = float(net_dd_r)
        samples = payload.get("concurrent_samples")
        if isinstance(samples, list):
            self._concurrent_samples = [int(v) for v in samples]

    def _pnl(self, is_long: bool, entry: float, exit_px: float) -> float:
        delta = (exit_px - entry) if is_long else (entry - exit_px)
        return delta * self.params.qty * self.params.point_value

    def _pnl_pips(self, is_long: bool, entry: float, exit_px: float) -> float:
        return pips_raw(
            exit_px=exit_px, entry=entry, pip_size=self.params.pip_size, is_long=is_long
        )

    def _weighted_pips(self, raw: float) -> float:
        return pips_weighted(raw, qty=self.params.qty, qty_ref=self.params.qty_ref)

    def _pair_weighted_pips(self, pair: Pair, raw: float) -> float:
        return pips_weighted(raw, qty=pair.qty, qty_ref=self.params.qty_ref)

    def _base_cost_schedule(self) -> CostSchedule:
        return CostSchedule(
            spread_pips_per_side=self.params.spread_pips_per_side,
            slippage_pips_per_side=self.params.slippage_pips_per_side,
            commission_pips_per_side=self.params.commission_pips_per_side,
            swap_long_pips_per_rollover=self.params.swap_long_pips_per_rollover,
            swap_short_pips_per_rollover=self.params.swap_short_pips_per_rollover,
        )

    def _cost_schedule(self, session: str) -> CostSchedule:
        return schedule_for(
            session=session,
            enabled=self.params.cost_model is not CostModel.NONE,
            base=self._base_cost_schedule(),
            overrides=self.params.session_cost_overrides,
        )

    def _leg_cost(
        self, pair: Pair, *, is_long: bool, as_of: datetime, exited: bool
    ) -> CostBreakdown:
        return leg_cost(
            schedule=self._cost_schedule(pair.session),
            entry_ts=pair.entry_ts,
            as_of=as_of,
            is_long=is_long,
            exited=exited,
            timezone=self.params.swap_timezone,
            rollover_time=self.params.swap_rollover_time,
            triple_weekday=self.params.swap_triple_weekday,
        )

    def _cost_accounting(
        self, mark: float, mark_ts: datetime | None = None
    ) -> CostAccounting:
        totals = CostAccounting()
        closed = {
            (leg.pair_id, leg.side): leg for leg in self.trades if leg.pair_id is not None
        }
        for pair in self.pairs:
            s_pips = pair.sl_dist / self.params.pip_size
            weight = pair.qty / self.params.qty_ref
            for is_long, side in ((True, "long"), (False, "short")):
                leg = closed.get((pair.id, side))
                is_open = pair.long_open if is_long else pair.short_open
                if leg is None and not is_open:
                    continue
                exited = leg is not None
                as_of = (
                    leg.ts
                    if leg is not None
                    else mark_ts
                    or (self.last_bar.ts if self.last_bar is not None else pair.entry_ts)
                )
                gross_raw = (
                    self._closed_leg_pips(leg)
                    if leg is not None
                    else self._pnl_pips(is_long, self._leg_entry(pair, is_long), mark)
                )
                costs = self._leg_cost(pair, is_long=is_long, as_of=as_of, exited=exited)
                gross_weighted = gross_raw * weight
                cost_weighted = costs.total_pips * weight
                execution_weighted = costs.execution_pips * weight
                financing_weighted = costs.financing_pips * weight
                sides = 2 if exited else 1
                schedule = self._cost_schedule(pair.session)
                if exited:
                    totals.gross_realized_pips += gross_weighted
                    totals.realized_cost_pips += cost_weighted
                    if s_pips > 0:
                        totals.gross_realized_r += gross_weighted / s_pips
                        totals.realized_cost_r += cost_weighted / s_pips
                    totals.realized_spread_cost_pips += (
                        schedule.spread_pips_per_side * sides * weight
                    )
                    totals.completed_transaction_sides += sides
                    totals.completed_side_equivalents += sides * weight
                else:
                    totals.gross_unrealized_pips += gross_weighted
                    totals.unrealized_cost_pips += cost_weighted
                    if s_pips > 0:
                        totals.gross_unrealized_r += gross_weighted / s_pips
                        totals.unrealized_cost_r += cost_weighted / s_pips
                totals.execution_cost_pips += execution_weighted
                totals.financing_cost_pips += financing_weighted
                totals.spread_cost_pips += schedule.spread_pips_per_side * sides * weight
                totals.transaction_sides += sides
                totals.side_equivalents += sides * weight
        return totals

    def _pips_to_dollars(self, pips: float) -> float | None:
        return cash(
            self._weighted_pips(pips),
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
        )

    def _closed_leg_pips(self, leg: ClosedLeg) -> float:
        if leg.pnl_pips is not None:
            return leg.pnl_pips
        return self._pnl_pips(leg.side == "long", leg.entry, leg.exit)

    def _unrealized_pips(self, mark: float) -> float:
        unrealized = 0.0
        for pair in self.pairs:
            if pair.long_open:
                unrealized += self._pnl_pips(True, self._leg_entry(pair, True), mark)
            if pair.short_open:
                unrealized += self._pnl_pips(False, self._leg_entry(pair, False), mark)
        return unrealized

    def _record_equity(self, mark: float, bar: Candle | None = None) -> None:
        self._mark_equity(mark, bar.ts if bar is not None else None)
        if bar is not None and self.m1_bars:
            for m1 in m1_covering(bar, self.m1_bars, self.params.timeframe_minutes):
                self._mark_equity(m1.close, m1.ts)

    def _mark_equity(self, mark: float, mark_ts: datetime | None = None) -> None:
        equity_pips = self._weighted_pips(
            self.stats.realized_pips + self._unrealized_pips(mark)
        )
        self.equity_peak_pips = max(self.equity_peak_pips, equity_pips)
        self.max_drawdown_pips = max(
            self.max_drawdown_pips, self.equity_peak_pips - equity_pips
        )
        equity_r = self._realized_r() + self._unrealized_r(mark)
        self.equity_peak_r = max(self.equity_peak_r, equity_r)
        self.max_drawdown_r = max(self.max_drawdown_r, self.equity_peak_r - equity_r)
        accounting = self._cost_accounting(mark, mark_ts)
        net_equity_pips = (
            accounting.gross_realized_pips
            + accounting.gross_unrealized_pips
            - accounting.realized_cost_pips
            - accounting.unrealized_cost_pips
        )
        net_equity_r = (
            accounting.gross_realized_r
            + accounting.gross_unrealized_r
            - accounting.realized_cost_r
            - accounting.unrealized_cost_r
        )
        self.net_equity_peak_pips = max(self.net_equity_peak_pips, net_equity_pips)
        self.net_max_drawdown_pips = max(
            self.net_max_drawdown_pips, self.net_equity_peak_pips - net_equity_pips
        )
        self.net_equity_peak_r = max(self.net_equity_peak_r, net_equity_r)
        self.net_max_drawdown_r = max(
            self.net_max_drawdown_r, self.net_equity_peak_r - net_equity_r
        )

    def _record_excursions(self, bar: Candle) -> None:
        for pair in self.pairs:
            if pair.long_open:
                entry = self._leg_entry(pair, True)
                pair.long_mae_pips = min(
                    pair.long_mae_pips, self._pnl_pips(True, entry, bar.low)
                )
                pair.long_mfe_pips = max(
                    pair.long_mfe_pips, self._pnl_pips(True, entry, bar.high)
                )
            if pair.short_open:
                entry = self._leg_entry(pair, False)
                pair.short_mae_pips = min(
                    pair.short_mae_pips, self._pnl_pips(False, entry, bar.high)
                )
                pair.short_mfe_pips = max(
                    pair.short_mfe_pips, self._pnl_pips(False, entry, bar.low)
                )

    def _bucket(self, is_long: bool, entry: float, exit_px: float) -> Literal["win", "be", "loss"]:
        pnl_px = (exit_px - entry) if is_long else (entry - exit_px)
        if pnl_px > self.be_eps:
            return "win"
        if pnl_px < -self.be_eps:
            return "loss"
        return "be"

    def _leg_entry(self, pair: Pair, is_long: bool) -> float:
        stored = pair.long_entry if is_long else pair.short_entry
        return pair.entry if stored is None else stored

    def _fill_pending(self, bar: Candle) -> None:
        if not self.pending:
            return
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for session, signal in list(self.pending.items()):
            if open_ts < signal.entry_time:
                continue
            del self.pending[session]
            self._open_pair(session, bar.open, signal.range_price, open_ts, signal.bullish)

    def _stop_distance(self, range_price: float) -> float:
        if self.params.stop_mode == StopMode.FIXED_PIPS:
            base = self.params.fixed_stop_pips * self.params.pip_size
        else:
            base = range_price * self.params.sl_mult
        return max(base, self.params.min_stop_pips * self.params.pip_size)

    def _open_pair(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> None:
        sl_dist = self._stop_distance(range_price)
        if sl_dist <= 0:
            return
        pair = Pair(
            id=f"{session}:{ts.isoformat()}",
            session=session,
            entry=entry,
            sl_dist=sl_dist,
            long_sl=entry - sl_dist,
            long_tp=entry + sl_dist * self.params.rr,
            short_sl=entry + sl_dist,
            short_tp=entry - sl_dist * self.params.rr,
            qty=self.params.qty,
            primary_side="long" if bullish else "short",
            entry_ts=ts,
            long_entry=entry,
            short_entry=entry,
        )
        self.pairs.append(pair)
        self._emit_entry(pair, ts, bullish_signal=bullish)

    def _emit_entry(self, pair: Pair, ts: datetime, *, bullish_signal: bool) -> None:
        self.events.append(
            EngineEvent(
                kind="entry",
                session=pair.session,
                ts=ts,
                detail={
                    "entry": pair.entry,
                    "sl_dist": pair.sl_dist,
                    "sl_pips": pair.sl_dist / self.params.pip_size,
                    "bullish_signal": bullish_signal,
                    "primary_side": pair.primary_side,
                    "pair_id": pair.id,
                },
            )
        )

    def _arm_signals(self, bar: Candle) -> None:
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for window in self.windows:
            in_now = window.contains(open_ts)
            self.prev_in_session[window.name] = in_now
            anchor = self.anchors_by_name.get(window.name)
            if anchor is None or not is_anchor_weekday(anchor, open_ts):
                continue
            anchor_ts = session_anchor_ts(anchor, open_ts)
            key = session_day_key(window.name, anchor_ts)
            orb_end = anchor_ts + timedelta(minutes=self.params.orb_minutes)
            collector = self.orb.get(window.name)

            if open_ts < anchor_ts:
                if collector is not None and collector.anchor_ts != anchor_ts:
                    self._feed_orb(collector, bar)
                continue

            if key in self._done:
                continue

            if open_ts >= orb_end:
                if collector is not None and collector.anchor_ts == anchor_ts:
                    self._feed_orb(collector, bar)
                else:
                    self._done.add(key)
                continue

            if collector is None or collector.anchor_ts != anchor_ts:
                collector = OrbCollector(session=window.name, anchor_ts=anchor_ts)
                self.orb[window.name] = collector
            self._feed_orb(collector, bar)

    def _feed_orb(self, collector: OrbCollector, bar: Candle) -> None:
        if collector.skipped:
            return
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        orb_end = collector.anchor_ts + timedelta(minutes=self.params.orb_minutes)
        in_window = collector.anchor_ts <= open_ts < orb_end
        if collector.first_open is None:
            if not in_window:
                if open_ts >= orb_end:
                    self._done.add(session_day_key(collector.session, collector.anchor_ts))
                    self.orb.pop(collector.session, None)
                return
            drift = drift_minutes(open_ts, collector.anchor_ts)
            self.anchor_drifts.setdefault(collector.session, []).append(drift)
            if drift > self.params.anchor_tolerance_minutes:
                collector.skipped = True
                self.anchor_skips[collector.session] = (
                    self.anchor_skips.get(collector.session, 0) + 1
                )
                self._done.add(session_day_key(collector.session, collector.anchor_ts))
                self.orb.pop(collector.session, None)
                self.events.append(
                    EngineEvent(
                        kind="signal_skipped_anchor_drift",
                        session=collector.session,
                        ts=bar.ts,
                        detail={
                            "anchor_ts": collector.anchor_ts.isoformat(),
                            "bar_open": open_ts.isoformat(),
                            "anchor_drift_minutes": drift,
                            "anchor_tolerance_minutes": self.params.anchor_tolerance_minutes,
                        },
                    )
                )
                return
            collector.first_open = open_ts
            collector.first_open_px = bar.open
        if in_window:
            collector.high = bar.high if collector.high is None else max(collector.high, bar.high)
            collector.low = bar.low if collector.low is None else min(collector.low, bar.low)
            collector.last_close = bar.close
        if bar.ts >= orb_end and collector.first_open is not None:
            self._complete_orb(collector, bar)

    def _complete_orb(self, collector: OrbCollector, bar: Candle) -> None:
        key = session_day_key(collector.session, collector.anchor_ts)
        self._done.add(key)
        self.orb.pop(collector.session, None)
        bars_range = None
        if collector.high is not None and collector.low is not None:
            bars_range = collector.high - collector.low
        if bars_range is None or bars_range <= 0:
            return
        is_doji = (
            self.params.skip_doji
            and collector.first_open_px is not None
            and collector.last_close is not None
            and collector.last_close == collector.first_open_px
        )
        if is_doji:
            return
        assert collector.first_open_px is not None
        assert collector.last_close is not None
        assert collector.first_open is not None
        bullish = collector.last_close > collector.first_open_px
        fill_at = entry_time(
            anchor_ts=collector.anchor_ts,
            orb_minutes=self.params.orb_minutes,
            entry_delay_minutes=self.params.entry_delay_minutes,
        )
        drift = drift_minutes(collector.first_open, collector.anchor_ts)
        self.pending[collector.session] = PendingSignal(
            session=collector.session,
            range_price=bars_range,
            bullish=bullish,
            signal_ts=bar.ts,
            entry_time=fill_at,
            anchor_drift_minutes=drift,
        )
        self.events.append(
            EngineEvent(
                kind="signal",
                session=collector.session,
                ts=bar.ts,
                detail={
                    "range": bars_range,
                    "bullish": bullish,
                    "anchor_ts": collector.anchor_ts.isoformat(),
                    "anchor_drift_minutes": drift,
                    "orb_minutes": self.params.orb_minutes,
                    "entry_time": fill_at.isoformat(),
                },
            )
        )

    def _session_anchor_stats(self) -> list[SessionAnchorStats]:
        stats: list[SessionAnchorStats] = []
        for window in self.windows:
            drifts = self.anchor_drifts.get(window.name, [])
            accepted = sum(
                1
                for event in self.events
                if event.kind == "signal" and event.session == window.name
            )
            closed = [pair for pair in self._closed_pairs() if pair.session == window.name]
            same_n = sum(1 for pair in closed if pair.same_bar_resolved)
            stats.append(
                SessionAnchorStats(
                    session=window.name,
                    skip_count=self.anchor_skips.get(window.name, 0),
                    signal_count=accepted,
                    anchor_drift_p50=percentile_50(drifts),
                    anchor_drift_max=max(drifts) if drifts else None,
                    anchor_drift_minutes=list(drifts),
                    same_bar_resolution_rate=(same_n / len(closed) if closed else 0.0),
                    same_bar_r=self._same_bar_r_for(closed),
                )
            )
        return stats

    def _closed_pairs(self) -> list[Pair]:
        return [pair for pair in self.pairs if not pair.long_open and not pair.short_open]

    def _same_bar_resolution_rate(self) -> float:
        closed = self._closed_pairs()
        if not closed:
            return 0.0
        return sum(1 for pair in closed if pair.same_bar_resolved) / len(closed)

    def _same_bar_r_for(self, pairs: list[Pair]) -> float:
        total = 0.0
        pip_size = self.params.pip_size
        for pair in pairs:
            if not pair.same_bar_resolved or pair.sl_dist <= 0:
                continue
            s_pips = pair.sl_dist / pip_size
            for leg in self.trades:
                if leg.pair_id == pair.id and leg.pnl_pips is not None:
                    total += r_multiple(leg.pnl_pips, s_pips=s_pips)
        return total

    def _same_bar_r(self) -> float:
        return self._same_bar_r_for(self._closed_pairs())

    def _pair_r(self, pair: Pair) -> float:
        s_pips = pair.sl_dist / self.params.pip_size
        if s_pips == 0:
            return 0.0
        total = 0.0
        for leg in self.trades:
            if leg.pair_id == pair.id and leg.pnl_pips is not None:
                total += r_multiple(leg.pnl_pips, s_pips=s_pips)
        return total

    def _realized_r(self) -> float:
        by_id = {pair.id: pair for pair in self.pairs}
        total = 0.0
        for leg in self.trades:
            pair = by_id.get(leg.pair_id or "")
            if pair is None or leg.pnl_pips is None or pair.sl_dist <= 0:
                continue
            total += r_multiple(leg.pnl_pips, s_pips=pair.sl_dist / self.params.pip_size)
        return total

    def _unrealized_r(self, mark: float) -> float:
        total = 0.0
        for pair in self.pairs:
            s_pips = pair.sl_dist / self.params.pip_size
            if s_pips == 0:
                continue
            if pair.long_open:
                raw = self._pnl_pips(True, self._leg_entry(pair, True), mark)
                total += r_multiple(raw, s_pips=s_pips)
            if pair.short_open:
                raw = self._pnl_pips(False, self._leg_entry(pair, False), mark)
                total += r_multiple(raw, s_pips=s_pips)
        return total

    def _headline_metrics(self):
        outcomes = []
        rs = []
        closed_by_pair_side = {
            (leg.pair_id, leg.side): leg for leg in self.trades if leg.pair_id is not None
        }
        for pair in self._closed_pairs():
            long_leg = closed_by_pair_side.get((pair.id, "long"))
            short_leg = closed_by_pair_side.get((pair.id, "short"))
            pair_r = self._pair_r(pair)
            outcomes.append(
                classify_pair(
                    locked=pair.locked,
                    same_bar=pair.same_bar_resolved,
                    long_bucket=long_leg.bucket if long_leg else None,
                    short_bucket=short_leg.bucket if short_leg else None,
                    pair_r=pair_r,
                )
            )
            rs.append(pair_r)
        return headline(
            outcomes=outcomes,
            r_multiples=rs,
            concurrent_samples=self._concurrent_samples,
        )

    def _manage_pairs(self, bar: Candle) -> None:
        for pair in self.pairs:
            if not pair.long_open and not pair.short_open:
                continue
            long_hit_sl = pair.long_open and bar.low <= pair.long_sl
            long_hit_tp = pair.long_open and bar.high >= pair.long_tp
            short_hit_sl = pair.short_open and bar.high >= pair.short_sl
            short_hit_tp = pair.short_open and bar.low <= pair.short_tp

            if not pair.locked and long_hit_sl and short_hit_sl:
                self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
                self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
            elif not pair.locked:
                if long_hit_sl:
                    self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
                    if pair.short_open:
                        self._apply_lock(pair, long_survives=False, ts=bar.ts)
                        self._resolve_after_lock(pair, bar, is_long=False)
                elif short_hit_sl:
                    self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
                    if pair.long_open:
                        self._apply_lock(pair, long_survives=True, ts=bar.ts)
                        self._resolve_after_lock(pair, bar, is_long=True)
                elif long_hit_tp:
                    self._close_long(pair, _fill_limit(bar.open, pair.long_tp, True), bar.ts)
                elif short_hit_tp:
                    self._close_short(pair, _fill_limit(bar.open, pair.short_tp, False), bar.ts)
            else:
                if pair.long_open:
                    if long_hit_sl:
                        self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
                    elif long_hit_tp:
                        self._close_long(pair, _fill_limit(bar.open, pair.long_tp, True), bar.ts)
                if pair.short_open:
                    if short_hit_sl:
                        self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
                    elif short_hit_tp:
                        self._close_short(
                            pair, _fill_limit(bar.open, pair.short_tp, False), bar.ts
                        )

    def _resolve_after_lock(self, pair: Pair, bar: Candle, *, is_long: bool) -> None:
        stop = pair.long_sl if is_long else pair.short_sl
        tp = pair.long_tp if is_long else pair.short_tp
        hit = after_lock_same_bar(
            mode=self.params.intrabar_mode,
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=tp,
            m1_bars=self.m1_bars,
            parent_minutes=self.params.timeframe_minutes,
        )
        if hit.kind == "none" or hit.fill is None:
            return
        if is_long:
            self._close_long(pair, hit.fill, bar.ts)
        else:
            self._close_short(pair, hit.fill, bar.ts)

    def _apply_lock(self, pair: Pair, *, long_survives: bool, ts: datetime) -> None:
        if pair.sl_dist >= self.lock_dist and self.lock_dist > 0:
            new_sl = pair.entry + self.lock_dist if long_survives else pair.entry - self.lock_dist
        else:
            new_sl = pair.entry
        if long_survives:
            pair.long_sl = new_sl
        else:
            pair.short_sl = new_sl
        pair.locked = True
        self.stats.locks += 1
        self.events.append(
            EngineEvent(
                kind="lock",
                session=pair.session,
                ts=ts,
                detail={"long_survives": long_survives, "new_sl": new_sl},
            )
        )

    def _close_long(self, pair: Pair, px: float, ts: datetime, reason: str = "sl_or_tp") -> None:
        if not pair.long_open:
            return
        self._record_close(pair, is_long=True, px=px, ts=ts, reason=reason)
        pair.long_open = False

    def _close_short(self, pair: Pair, px: float, ts: datetime, reason: str = "sl_or_tp") -> None:
        if not pair.short_open:
            return
        self._record_close(pair, is_long=False, px=px, ts=ts, reason=reason)
        pair.short_open = False

    def _record_close(
        self, pair: Pair, *, is_long: bool, px: float, ts: datetime, reason: str
    ) -> None:
        side: Literal["long", "short"] = "long" if is_long else "short"
        entry = self._leg_entry(pair, is_long)
        bucket = self._bucket(is_long, entry, px)
        pnl = self._pnl(is_long, entry, px)
        pnl_pips = self._pnl_pips(is_long, entry, px)
        pnl_dollars = self._pips_to_dollars(pnl_pips)
        costs = self._leg_cost(pair, is_long=is_long, as_of=ts, exited=True)
        weighted_cost = costs.total_pips * (pair.qty / self.params.qty_ref)
        weighted_gross = self._pair_weighted_pips(pair, pnl_pips)
        mae_pips = pair.long_mae_pips if is_long else pair.short_mae_pips
        mfe_pips = pair.long_mfe_pips if is_long else pair.short_mfe_pips
        role: Literal["primary", "hedge", "unknown"]
        if pair.primary_side is None:
            role = "unknown"
        else:
            role = "primary" if side == pair.primary_side else "hedge"
        self.stats.realized += pnl
        self.stats.realized_pips += pnl_pips
        if pair.first_close_ts is None:
            pair.first_close_ts = ts
        elif pair.first_close_ts == ts:
            pair.same_bar_resolved = True
        if is_long:
            if bucket == "win":
                self.stats.long_wins += 1
            elif bucket == "be":
                self.stats.long_be += 1
            else:
                self.stats.long_loss += 1
        elif bucket == "win":
            self.stats.short_wins += 1
        elif bucket == "be":
            self.stats.short_be += 1
        else:
            self.stats.short_loss += 1
        self.trades.append(
            ClosedLeg(
                session=pair.session,
                side=side,
                entry=entry,
                exit=px,
                pnl=pnl,
                bucket=bucket,
                ts=ts,
                reason=reason,
                pair_id=pair.id,
                role=role,
                entry_ts=pair.entry_ts,
                pnl_pips=pnl_pips,
                pnl_dollars=pnl_dollars,
                gross_pnl_pips=weighted_gross,
                cost_pips=weighted_cost,
                net_pnl_pips=weighted_gross - weighted_cost,
                mae_pips=mae_pips,
                mfe_pips=mfe_pips,
                mae_dollars=self._pips_to_dollars(mae_pips),
                mfe_dollars=self._pips_to_dollars(mfe_pips),
            )
        )
        self.events.append(
            EngineEvent(
                kind="exit",
                session=pair.session,
                ts=ts,
                detail={
                    "side": side,
                    "exit": px,
                    "bucket": bucket,
                    "pnl": pnl,
                    "pnl_pips": pnl_pips,
                    "pnl_dollars": pnl_dollars,
                    "mae_pips": mae_pips,
                    "mfe_pips": mfe_pips,
                    "mae_dollars": self._pips_to_dollars(mae_pips),
                    "mfe_dollars": self._pips_to_dollars(mfe_pips),
                    "pair_id": pair.id,
                    "role": role,
                    "reason": reason,
                },
            )
        )

    def _trade_pair_results(self, mark: float) -> list[TradePairResult]:
        closed_by_pair_side = {
            (leg.pair_id, leg.side): leg for leg in self.trades if leg.pair_id is not None
        }
        results: list[TradePairResult] = []
        for pair in self.pairs:
            long_leg = self._pair_leg_result(
                pair, True, mark, closed_by_pair_side.get((pair.id, "long"))
            )
            short_leg = self._pair_leg_result(
                pair, False, mark, closed_by_pair_side.get((pair.id, "short"))
            )
            open_count = int(pair.long_open) + int(pair.short_open)
            status: Literal["open", "partial", "closed"]
            if open_count == 2:
                status = "open"
            elif open_count == 1:
                status = "partial"
            else:
                status = "closed"
            legs = [long_leg, short_leg]
            pnl_pips = sum(leg.pnl_pips for leg in legs)
            gross_pnl_pips = sum(
                leg.gross_pnl_pips if leg.gross_pnl_pips is not None else leg.pnl_pips
                for leg in legs
            )
            cost_pips = sum(leg.cost_pips for leg in legs)
            if pair.primary_side == "long":
                primary, hedge, unknown = long_leg, short_leg, []
            elif pair.primary_side == "short":
                primary, hedge, unknown = short_leg, long_leg, []
            else:
                primary, hedge, unknown = None, None, legs
            results.append(
                TradePairResult(
                    id=pair.id,
                    session=pair.session,
                    entry=pair.entry,
                    entry_ts=pair.entry_ts,
                    status=status,
                    primary=primary,
                    hedge=hedge,
                    unknown_legs=unknown,
                    pnl_pips=pnl_pips,
                    pnl_dollars=self._pips_to_dollars(pnl_pips),
                    gross_pnl_pips=gross_pnl_pips,
                    cost_pips=cost_pips,
                    net_pnl_pips=gross_pnl_pips - cost_pips,
                )
            )
        return results

    def _pair_leg_result(
        self, pair: Pair, is_long: bool, mark: float, closed: ClosedLeg | None
    ) -> TradePairLeg:
        side: Literal["long", "short"] = "long" if is_long else "short"
        if pair.primary_side is None:
            role: Literal["primary", "hedge", "unknown"] = "unknown"
        else:
            role = "primary" if side == pair.primary_side else "hedge"
        if closed is not None:
            pnl_pips = self._closed_leg_pips(closed)
            costs = self._leg_cost(pair, is_long=is_long, as_of=closed.ts, exited=True)
            gross_weighted = self._pair_weighted_pips(pair, pnl_pips)
            cost_weighted = costs.total_pips * (pair.qty / self.params.qty_ref)
            mae_pips = (
                closed.mae_pips
                if closed.mae_pips is not None
                else (pair.long_mae_pips if is_long else pair.short_mae_pips)
            )
            mfe_pips = (
                closed.mfe_pips
                if closed.mfe_pips is not None
                else (pair.long_mfe_pips if is_long else pair.short_mfe_pips)
            )
            return TradePairLeg(
                side=side,
                role=role,
                status="closed",
                exit=closed.exit,
                exit_ts=closed.ts,
                pnl_pips=pnl_pips,
                pnl_dollars=self._pips_to_dollars(pnl_pips),
                mae_pips=mae_pips,
                mfe_pips=mfe_pips,
                mae_dollars=self._pips_to_dollars(mae_pips),
                mfe_dollars=self._pips_to_dollars(mfe_pips),
                bucket=closed.bucket,
                reason=closed.reason,
                gross_pnl_pips=gross_weighted,
                cost_pips=cost_weighted,
                net_pnl_pips=gross_weighted - cost_weighted,
            )
        opened = pair.long_open if is_long else pair.short_open
        if not opened:
            return TradePairLeg(
                side=side,
                role=role,
                status="closed",
                pnl_pips=0.0,
                pnl_dollars=self._pips_to_dollars(0.0),
                mae_dollars=self._pips_to_dollars(0.0),
                mfe_dollars=self._pips_to_dollars(0.0),
                reason="not_filled",
            )
        pnl_pips = self._pnl_pips(is_long, self._leg_entry(pair, is_long), mark)
        as_of = self.last_bar.ts if self.last_bar is not None else pair.entry_ts
        costs = self._leg_cost(pair, is_long=is_long, as_of=as_of, exited=False)
        gross_weighted = self._pair_weighted_pips(pair, pnl_pips)
        cost_weighted = costs.total_pips * (pair.qty / self.params.qty_ref)
        mae_pips = pair.long_mae_pips if is_long else pair.short_mae_pips
        mfe_pips = pair.long_mfe_pips if is_long else pair.short_mfe_pips
        return TradePairLeg(
            side=side,
            role=role,
            status="open",
            pnl_pips=pnl_pips,
            pnl_dollars=self._pips_to_dollars(pnl_pips),
            mae_pips=mae_pips,
            mfe_pips=mfe_pips,
            mae_dollars=self._pips_to_dollars(mae_pips),
            mfe_dollars=self._pips_to_dollars(mfe_pips),
            gross_pnl_pips=gross_weighted,
            cost_pips=cost_weighted,
            net_pnl_pips=gross_weighted - cost_weighted,
        )
