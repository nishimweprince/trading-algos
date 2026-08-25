"""Closed-bar session-open hedge engine. Shared by backtest and paper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
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
    cost_derived_min_stop_pips,
    effective_min_stop_pips,
    headroom_ratio,
    rollover_units,
    schedule_for,
)
from entry import hedge_pair_plan, synthetic_order_plan
from exits import initial_target_r, target_price, time_exit_due
from fills import (
    OcoTriggerHit,
    TickPathUnavailable,
    after_lock_same_bar,
    covering_status,
    m1_covering,
    resolve_bar_levels,
    resolve_oco_trigger,
)
from filters import (
    NR7_LOOKBACK,
    DailyCloseTracker,
    entry_filter_reason,
)
from firm_profile import FirmProfile, firm_identity
from indicators import (
    ATR14_PERIOD,
    ATR_HISTORY_BARS,
    blended_orb_atr,
    wilder_atr,
)
from metrics import bucket_win_rate, classify_pair, headline, percentile
from models import (
    BacktestReport,
    BacktestReportHeader,
    Candle,
    ClosedLeg,
    CostModel,
    EngineEvent,
    EngineParams,
    EntryMode,
    EquityCurvePoint,
    FirmProfileMode,
    IntrabarMode,
    LockMode,
    OcoBufferMode,
    OpenEntryOrderView,
    OpenPairView,
    OutcomeMix,
    PerformanceUnit,
    PerformanceView,
    RiskMode,
    SessionAnchorStats,
    Stats,
    StopMode,
    TargetMode,
    Timeframe,
    TradePairLeg,
    TradePairResult,
)
from risk_guards import PropGuard
from sessions import SessionWindow
from sizing import SizingDecision, fixed_fractional_size, fixed_qty_size
from units import cash, conversion_factor, pips_raw, pips_weighted, r_multiple
from validation import GAP, validate_bar


@dataclass
class PendingSignal:
    session: str
    range_price: float
    bullish: bool
    signal_ts: datetime
    entry_time: datetime
    anchor_drift_minutes: float = 0.0
    range_high: float | None = None
    range_low: float | None = None


@dataclass
class EntryOrder:
    id: str
    session: str
    mode: EntryMode
    reference_entry: float
    sl_dist: float
    upper_trigger: float
    lower_trigger: float
    bullish: bool
    staged_ts: datetime
    qty: float
    initial_risk_pct: float | None
    initial_risk_cash: float | None
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    expiry_bars: int | None = None
    bars_seen: int = 0
    reentry_index: int = 0
    root_id: str | None = None


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
class EntryLot:
    ts: datetime
    qty: float


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
    long_qty: float | None = None
    short_qty: float | None = None
    long_entry_fills: int = 1
    short_entry_fills: int = 1
    long_episode: int = 0
    short_episode: int = 0
    initial_risk_pct: float | None = None
    initial_risk_cash: float | None = None
    primary_side: Literal["long", "short"] | None = None
    long_open: bool = True
    short_open: bool = True
    locked: bool = False
    entry_ts: datetime = field(default_factory=datetime.now)
    long_entry: float | None = None
    short_entry: float | None = None
    long_entry_ts: datetime | None = None
    short_entry_ts: datetime | None = None
    long_entry_lots: list[EntryLot] = field(default_factory=list)
    short_entry_lots: list[EntryLot] = field(default_factory=list)
    long_mae_pips: float = 0.0
    long_mfe_pips: float = 0.0
    short_mae_pips: float = 0.0
    short_mfe_pips: float = 0.0
    first_close_ts: datetime | None = None
    same_bar_resolved: bool = False
    reference_entry: float | None = None
    entry_gap: bool = False
    exit_gap: bool = False
    entry_ambiguous: bool = False
    entry_bar_close_ts: datetime | None = None
    entry_m1_index: int | None = None
    contingent_initial_ratio: float | None = None
    hedge_failure_threshold: float | None = None
    hedge_ratio_staged: float = 0.0
    hedge_staged: bool = False
    entry_mode: EntryMode = EntryMode.HEDGE_PAIR
    reentry_index: int = 0
    root_id: str | None = None
    bracket_upper: float | None = None
    bracket_lower: float | None = None
    reentry_staged: bool = False
    bullish_signal: bool = True
    long_partial_taken: bool = False
    short_partial_taken: bool = False


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


def _entry_lots(value: object) -> list[EntryLot]:
    if not isinstance(value, list):
        return []
    lots: list[EntryLot] = []
    for raw in value:
        if isinstance(raw, dict) and raw.get("ts") is not None:
            lots.append(
                EntryLot(
                    ts=datetime.fromisoformat(str(raw["ts"])),
                    qty=float(raw["qty"]),
                )
            )
    return lots


SNAPSHOT_VERSION = 1


def infer_primary_side(
    stored: object, *, long_open: bool, short_open: bool
) -> Literal["long", "short"] | None:
    """Restore a missing primary-side from which leg is still open."""
    if stored in {"long", "short"}:
        return stored  # type: ignore[return-value]
    if long_open == short_open:
        return None
    return "long" if not long_open else "short"


class ClosedBarEngine:
    """One step per closed bar of the configured timeframe. Fill is time-based."""

    def __init__(
        self,
        windows: list[SessionWindow],
        params: EngineParams,
        anchors: list[SessionAnchor] | None = None,
        m1_bars: list[Candle] | None = None,
        *,
        collect_equity_curve: bool = False,
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
        self.entry_orders: list[EntryOrder] = []
        self.pending: dict[str, PendingSignal] = {}
        self.orb: dict[str, OrbCollector] = {}
        self._done: set[str] = set()
        self.anchor_drifts: dict[str, list[float]] = {window.name: [] for window in windows}
        self.anchor_skips: dict[str, int] = {window.name: 0 for window in windows}
        self.prev_in_session: dict[str, bool] = {window.name: False for window in windows}
        self.stats = Stats()
        self.trades: list[ClosedLeg] = []
        self.events: list[EngineEvent] = []
        self.first_bar: Candle | None = None
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
        self._collect_equity_curve = collect_equity_curve
        self._equity_curve_pips: dict[datetime, tuple[float, float]] = {}
        self._concurrent_samples: list[int] = []
        self.suppressed_signal_count = 0
        self.suppressed_signal_reasons: dict[str, int] = {}
        self._warmup_bars_consumed = 0
        self._m1_resolver_calls = 0
        self._m1_covered_resolver_calls = 0
        self._m1_partial_coverage_count = 0
        self._m1_fallback_count = 0
        self._bars: list[Candle] = []
        self._d1 = DailyCloseTracker()
        self._orb_ranges: dict[str, list[float]] = {window.name: [] for window in windows}
        self.trades_skipped_by_filter = 0
        self.non_positive_stop_count = 0
        firm_profile = None
        if params.firm_profile is FirmProfileMode.CUSTOM:
            firm_profile = FirmProfile(
                initial_balance=(
                    params.firm_initial_balance
                    if params.firm_initial_balance is not None
                    else params.initial_capital
                ),
                daily_loss_limit_pct=params.firm_daily_loss_limit_pct,
                total_loss_limit_pct=params.firm_total_loss_limit_pct,
                timezone=params.firm_timezone,
                daily_reset_time=params.firm_daily_reset_time,
            )
        self.prop_guard = PropGuard(firm_profile)

    def observe(self, bar: Candle) -> None:
        """Record session membership without trading. Used to warm paper on first tick."""
        if self.first_bar is None:
            self.first_bar = bar
            self._warmup_bars_consumed += 1
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
        self._d1.observe(bar)
        self.last_bar = bar
        self._remember_bar(bar)

    def _remember_bar(self, bar: Candle) -> None:
        if self._bars and self._bars[-1].ts == bar.ts:
            return
        self._bars.append(bar)
        overflow = len(self._bars) - ATR_HISTORY_BARS
        if overflow > 0:
            del self._bars[:overflow]

    def _mark_elapsed_sessions(self, bar: Candle) -> None:
        """Mark session-days whose ORB window is already over. Backtest warmup."""
        self._warmup_bars_consumed += 1
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

    def _note_resolver_coverage(self, bar: Candle) -> None:
        """Count M1 fallback decisions at each resolver call site, including partial coverage."""
        if self.params.intrabar_mode not in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE}:
            return
        covering = m1_covering(bar, self.m1_bars, self.params.timeframe_minutes)
        self._m1_resolver_calls += 1
        status = covering_status(covering, self.params.timeframe_minutes)
        if status == "complete":
            self._m1_covered_resolver_calls += 1
        elif status == "partial":
            self._m1_partial_coverage_count += 1
            self._m1_fallback_count += 1
        else:
            self._m1_fallback_count += 1

    def _resolve_bar_levels(self, *, is_long: bool, bar: Candle, stop: float, tp: float):
        self._note_resolver_coverage(bar)
        return resolve_bar_levels(
            mode=self.params.intrabar_mode,
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=tp,
            m1_bars=self.m1_bars,
            parent_minutes=self.params.timeframe_minutes,
        )

    def _after_lock_same_bar(self, *, is_long: bool, bar: Candle, stop: float, tp: float):
        self._note_resolver_coverage(bar)
        return after_lock_same_bar(
            mode=self.params.intrabar_mode,
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=tp,
            m1_bars=self.m1_bars,
            parent_minutes=self.params.timeframe_minutes,
        )

    def _resolve_oco_trigger(
        self, *, bullish_signal: bool, bar: Candle, upper: float, lower: float
    ):
        self._note_resolver_coverage(bar)
        return resolve_oco_trigger(
            mode=self.params.intrabar_mode,
            bullish_signal=bullish_signal,
            bar=bar,
            upper=upper,
            lower=lower,
            m1_bars=self.m1_bars,
            parent_minutes=self.params.timeframe_minutes,
        )

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
        if self.first_bar is None:
            self.first_bar = bar
        self._d1.observe(bar)
        self._fill_pending(bar)
        self._fill_entry_orders(bar)
        self._stage_contingent_hedges(bar)
        self._record_excursions(bar)
        self._manage_pairs(bar)
        self._stage_oco_reentries(bar)
        self._arm_signals(bar)
        self.last_bar = bar
        active_ids = {pair.id for pair in self.pairs if pair.long_open or pair.short_open} | {
            order.id for order in self.entry_orders
        }
        open_count = len(active_ids)
        self._concurrent_samples.append(open_count)
        self._record_equity(bar.close, bar)
        self._remember_bar(bar)
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
                unrealized += self._pnl(
                    True,
                    self._leg_entry(pair, True),
                    last_close,
                    qty=self._leg_qty(pair, True),
                )
            if pair.short_open:
                unrealized += self._pnl(
                    False,
                    self._leg_entry(pair, False),
                    last_close,
                    qty=self._leg_qty(pair, False),
                )
        accounting = self._cost_accounting(
            last_close, self.last_bar.ts if self.last_bar is not None else None
        )
        unrealized_pips = accounting.gross_unrealized_pips
        realized_r = accounting.gross_realized_r
        unrealized_r = accounting.gross_unrealized_r
        gross_equity_pips = accounting.gross_realized_pips + accounting.gross_unrealized_pips
        equity_cost_pips = accounting.realized_cost_pips + accounting.unrealized_cost_pips
        gross_equity_r = accounting.gross_realized_r + accounting.gross_unrealized_r
        equity_cost_r = accounting.realized_cost_r + accounting.unrealized_cost_r
        equity_pips = gross_equity_pips
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
        realized_dollars = self._pips_to_dollars(accounting.gross_realized_pips)
        unrealized_dollars = self._pips_to_dollars(unrealized_pips)
        max_drawdown_dollars = self._pips_to_dollars(self.max_drawdown_pips)
        open_pairs = sum(1 for pair in self.pairs if pair.long_open or pair.short_open)
        metrics = self._headline_metrics()
        validation_summary: dict[str, int] = {}
        for event in self.events:
            if event.kind != "bar_skipped_invalid":
                continue
            reason = str(event.detail.get("reason", "unknown"))
            validation_summary[reason] = validation_summary.get(reason, 0) + 1
        anchors = [
            f"{anchor.name}:{anchor.tz.key}:{anchor.at.strftime('%H:%M')}"
            for anchor in self.anchors_by_name.values()
        ]
        resolver_tiers = {
            IntrabarMode.OPTIMISTIC: 0,
            IntrabarMode.PESSIMISTIC: 1,
            IntrabarMode.M1: 2,
            IntrabarMode.M1_CONSERVATIVE: 3,
            IntrabarMode.TICK: 4,
        }
        m1_fallback_count = self._m1_fallback_count
        firm_profile_name, firm_profile_version = firm_identity(
            self.params.firm_profile is FirmProfileMode.CUSTOM
        )
        trade_pairs = self._trade_pair_results(last_close)
        hold_values = [pair.hold_hours for pair in trade_pairs if pair.hold_hours is not None]
        wins = self.stats.long_wins + self.stats.short_wins
        be = self.stats.long_be + self.stats.short_be
        loss = self.stats.long_loss + self.stats.short_loss
        return BacktestReport(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            bar_count=0,
            performance_unit=self.params.performance_unit,
            entry_mode=self.params.entry_mode,
            orb_minutes=self.params.orb_minutes,
            entry_delay_minutes=self.params.entry_delay_minutes,
            anchor_tolerance_minutes=self.params.anchor_tolerance_minutes,
            stop_mode=self.params.stop_mode,
            fixed_stop_pips=self.params.fixed_stop_pips,
            realized=self.stats.realized,
            unrealized=unrealized,
            equity=equity_pips,
            realized_pips=accounting.gross_realized_pips,
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
            net_realized_pips=(accounting.gross_realized_pips - accounting.realized_cost_pips),
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
            net_unrealized_r=(accounting.gross_unrealized_r - accounting.unrealized_cost_r),
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
            risk_mode=self.params.risk_mode,
            suppressed_signal_count=self.suppressed_signal_count,
            suppressed_signal_reasons=dict(self.suppressed_signal_reasons),
            trades_skipped_by_filter=self.trades_skipped_by_filter,
            non_positive_stop_count=self.non_positive_stop_count,
            firm_profile=self.params.firm_profile,
            firm_profile_name=firm_profile_name,
            firm_profile_version=firm_profile_version,
            prop_guard_breached=self.prop_guard.state.breached,
            prop_guard_breach_reason=self.prop_guard.state.breach_reason,
            prop_guard_breached_at=self.prop_guard.state.breached_at,
            prop_guard_daily_reference_equity=(self.prop_guard.state.daily_reference_equity),
            prop_guard_last_equity_cash=self.prop_guard.state.last_equity_cash,
            time_exit_mode=self.params.time_exit_mode,
            max_age_hours=self.params.max_age_hours,
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
            pending_entry_orders=len(self.entry_orders),
            unresolved_structures=len(
                {pair.id for pair in self.pairs if pair.long_open or pair.short_open}
                | {order.id for order in self.entry_orders}
            ),
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
                time_exit=metrics.outcome_mix.time_exit,
            ),
            performance=self._performance_view(
                accounting=accounting,
                unrealized=unrealized_pips,
                equity_pips=equity_pips,
                breakeven_pips_per_side=breakeven,
                configured_spread_per_side=configured_spread,
                configured_execution_cost_per_side=configured_execution,
            ),
            report_header=BacktestReportHeader(
                entry_mode=self.params.entry_mode,
                session_anchors=anchors,
                stop_mode=self.params.stop_mode,
                tp_mode=self.params.tp_mode,
                rr=self.params.rr,
                partial_tp_r=self.params.partial_tp_r,
                partial_fraction=self.params.partial_fraction,
                lock_mode=self.params.lock_mode,
                lock_pips=self.params.lock_pips,
                lock_r=self.params.lock_r,
                min_stop_pips=self.params.min_stop_pips,
                min_stop_cost_mult=self.params.min_stop_cost_mult,
                derived_min_stop_pips=(
                    cost_derived_min_stop_pips(
                        self._base_cost_schedule()
                        if self.params.cost_model is not CostModel.NONE
                        else CostSchedule(),
                        self.params.min_stop_cost_mult,
                    )
                    if self.params.min_stop_cost_mult > 0
                    else None
                ),
                filter_d1_ema50=self.params.filter_d1_ema50,
                filter_nr7=self.params.filter_nr7,
                filter_orb_atr_min=self.params.filter_orb_atr_min,
                filter_orb_atr_max=self.params.filter_orb_atr_max,
                time_exit_mode=self.params.time_exit_mode,
                max_age_hours=self.params.max_age_hours,
                risk_mode=self.params.risk_mode,
                cost_model=self.params.cost_model,
                intrabar_mode=self.params.intrabar_mode,
                resolver_tier=resolver_tiers[self.params.intrabar_mode],
                qty_ref=self.params.qty_ref,
                firm_profile=self.params.firm_profile,
                firm_profile_name=firm_profile_name,
                firm_profile_version=firm_profile_version,
                first_bar_ts=self.first_bar.ts if self.first_bar is not None else None,
                last_bar_ts=self.last_bar.ts if self.last_bar is not None else None,
                warmup_bars=self._warmup_bars_consumed,
                validation_summary=validation_summary,
                m1_bars_loaded=len(self.m1_bars),
                m1_resolver_calls=self._m1_resolver_calls,
                m1_covered_resolver_calls=self._m1_covered_resolver_calls,
                m1_partial_coverage_count=self._m1_partial_coverage_count,
                m1_fallback_count=m1_fallback_count,
            ),
            equity_curve=self._equity_curve(),
            max_concurrent_structures=metrics.max_concurrent_structures,
            median_concurrent=metrics.median_concurrent,
            win_rate=bucket_win_rate(wins, be, loss, exclude_be=False),
            win_rate_excl_be=bucket_win_rate(wins, be, loss, exclude_be=True),
            median_hold_hours=float(median(hold_values)) if hold_values else None,
            p95_hold_hours=percentile(hold_values, 0.95),
            trades=list(self.trades),
            trade_pairs=trade_pairs,
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
                    qty=pair.qty,
                    initial_risk_pct=pair.initial_risk_pct,
                    initial_risk_cash=pair.initial_risk_cash,
                )
            )
        return views

    def open_entry_order_views(self) -> list[OpenEntryOrderView]:
        return [
            OpenEntryOrderView(
                id=order.id,
                session=order.session,
                entry_mode=order.mode,
                reference_entry=order.reference_entry,
                sl_dist=order.sl_dist,
                upper_trigger=order.upper_trigger,
                lower_trigger=order.lower_trigger,
                staged_ts=order.staged_ts,
                qty=order.qty,
                expiry_bars=order.expiry_bars,
                bars_seen=order.bars_seen,
                reentry_index=order.reentry_index,
            )
            for order in self.entry_orders
        ]

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": SNAPSHOT_VERSION,
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
                    "range_high": signal.range_high,
                    "range_low": signal.range_low,
                }
                for name, signal in self.pending.items()
            },
            "entry_orders": [
                asdict(order) | {"mode": order.mode.value, "staged_ts": order.staged_ts.isoformat()}
                for order in self.entry_orders
            ],
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
            "suppressed_signal_count": self.suppressed_signal_count,
            "suppressed_signal_reasons": dict(self.suppressed_signal_reasons),
            "trades_skipped_by_filter": self.trades_skipped_by_filter,
            "non_positive_stop_count": self.non_positive_stop_count,
            "d1": self._d1.snapshot(),
            "orb_ranges": {name: list(values) for name, values in self._orb_ranges.items()},
            "prop_guard": self.prop_guard.snapshot(),
            "first_bar": self.first_bar.model_dump(mode="json") if self.first_bar else None,
            "last_bar": self.last_bar.model_dump(mode="json") if self.last_bar else None,
            "bars": [candle.model_dump(mode="json") for candle in self._bars],
        }

    def restore(self, payload: dict[str, object]) -> None:
        first_bar_raw = payload.get("first_bar")
        if isinstance(first_bar_raw, dict):
            self.first_bar = Candle.model_validate(first_bar_raw)
        last_bar_raw = payload.get("last_bar")
        if isinstance(last_bar_raw, dict):
            self.last_bar = Candle.model_validate(last_bar_raw)
        bars_raw = payload.get("bars")
        self._bars = []
        if isinstance(bars_raw, list):
            for raw in bars_raw:
                if isinstance(raw, dict):
                    self._bars.append(Candle.model_validate(raw))
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
                    range_high=_optional_float(raw.get("range_high")),
                    range_low=_optional_float(raw.get("range_low")),
                )
        self.entry_orders = []
        orders_raw = payload.get("entry_orders")
        if isinstance(orders_raw, list):
            for raw in orders_raw:
                if not isinstance(raw, dict):
                    continue
                self.entry_orders.append(
                    EntryOrder(
                        id=str(raw["id"]),
                        session=str(raw["session"]),
                        mode=EntryMode(str(raw["mode"])),
                        reference_entry=float(raw["reference_entry"]),
                        sl_dist=float(raw["sl_dist"]),
                        upper_trigger=float(raw["upper_trigger"]),
                        lower_trigger=float(raw["lower_trigger"]),
                        bullish=bool(raw["bullish"]),
                        staged_ts=datetime.fromisoformat(str(raw["staged_ts"])),
                        qty=float(raw["qty"]),
                        initial_risk_pct=_optional_float(raw.get("initial_risk_pct")),
                        initial_risk_cash=_optional_float(raw.get("initial_risk_cash")),
                        long_sl=float(raw["long_sl"]),
                        long_tp=float(raw["long_tp"]),
                        short_sl=float(raw["short_sl"]),
                        short_tp=float(raw["short_tp"]),
                        expiry_bars=(
                            int(raw["expiry_bars"]) if raw.get("expiry_bars") is not None else None
                        ),
                        bars_seen=int(raw.get("bars_seen", 0)),
                        reentry_index=int(raw.get("reentry_index", 0)),
                        root_id=str(raw["root_id"]) if raw.get("root_id") else None,
                    )
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
                        long_qty=_optional_float(raw.get("long_qty")),
                        short_qty=_optional_float(raw.get("short_qty")),
                        long_entry_fills=int(raw.get("long_entry_fills", 1)),
                        short_entry_fills=int(raw.get("short_entry_fills", 1)),
                        long_episode=int(raw.get("long_episode", 0)),
                        short_episode=int(raw.get("short_episode", 0)),
                        initial_risk_pct=_optional_float(raw.get("initial_risk_pct")),
                        initial_risk_cash=_optional_float(raw.get("initial_risk_cash")),
                        primary_side=infer_primary_side(
                            raw.get("primary_side"),
                            long_open=bool(raw["long_open"]),
                            short_open=bool(raw["short_open"]),
                        ),
                        long_open=bool(raw["long_open"]),
                        short_open=bool(raw["short_open"]),
                        locked=bool(raw["locked"]),
                        entry_ts=datetime.fromisoformat(str(raw["entry_ts"])),
                        long_entry=_optional_float(raw.get("long_entry")),
                        short_entry=_optional_float(raw.get("short_entry")),
                        long_entry_ts=(
                            datetime.fromisoformat(str(raw["long_entry_ts"]))
                            if raw.get("long_entry_ts")
                            else None
                        ),
                        short_entry_ts=(
                            datetime.fromisoformat(str(raw["short_entry_ts"]))
                            if raw.get("short_entry_ts")
                            else None
                        ),
                        long_entry_lots=_entry_lots(raw.get("long_entry_lots")),
                        short_entry_lots=_entry_lots(raw.get("short_entry_lots")),
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
                        reference_entry=_optional_float(raw.get("reference_entry")),
                        entry_gap=bool(raw.get("entry_gap", False)),
                        exit_gap=bool(raw.get("exit_gap", False)),
                        entry_ambiguous=bool(raw.get("entry_ambiguous", False)),
                        entry_bar_close_ts=(
                            datetime.fromisoformat(str(raw["entry_bar_close_ts"]))
                            if raw.get("entry_bar_close_ts")
                            else None
                        ),
                        entry_m1_index=(
                            int(raw["entry_m1_index"])
                            if raw.get("entry_m1_index") is not None
                            else None
                        ),
                        contingent_initial_ratio=_optional_float(
                            raw.get("contingent_initial_ratio")
                        ),
                        hedge_failure_threshold=_optional_float(raw.get("hedge_failure_threshold")),
                        hedge_ratio_staged=float(raw.get("hedge_ratio_staged", 0.0)),
                        hedge_staged=bool(raw.get("hedge_staged", False)),
                        entry_mode=EntryMode(str(raw.get("entry_mode", "hedge_pair"))),
                        reentry_index=int(raw.get("reentry_index", 0)),
                        root_id=str(raw["root_id"]) if raw.get("root_id") else None,
                        bracket_upper=_optional_float(raw.get("bracket_upper")),
                        bracket_lower=_optional_float(raw.get("bracket_lower")),
                        reentry_staged=bool(raw.get("reentry_staged", False)),
                        bullish_signal=bool(raw.get("bullish_signal", True)),
                        long_partial_taken=bool(raw.get("long_partial_taken", False)),
                        short_partial_taken=bool(raw.get("short_partial_taken", False)),
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
            pairs_by_id = {pair.id: pair for pair in self.pairs}
            self.stats.realized_pips = sum(
                self._pair_weighted_pips(pair, self._closed_leg_pips(leg))
                if (pair := pairs_by_id.get(leg.pair_id or "")) is not None
                else self._weighted_pips(self._closed_leg_pips(leg))
                for leg in self.trades
            )
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
        self.suppressed_signal_count = int(payload.get("suppressed_signal_count", 0))
        reasons = payload.get("suppressed_signal_reasons")
        if isinstance(reasons, dict):
            self.suppressed_signal_reasons = {str(k): int(v) for k, v in reasons.items()}
        self.trades_skipped_by_filter = int(payload.get("trades_skipped_by_filter", 0))
        self.non_positive_stop_count = int(payload.get("non_positive_stop_count", 0))
        self._d1.restore(payload.get("d1"))
        orb_ranges = payload.get("orb_ranges")
        if isinstance(orb_ranges, dict):
            self._orb_ranges = {
                str(name): [float(v) for v in values]
                for name, values in orb_ranges.items()
                if isinstance(values, list)
            }
        self.prop_guard.restore(payload.get("prop_guard"))

    def prune_closed_history(
        self,
        *,
        max_closed_pairs: int,
        max_events: int,
        max_trades: int,
        max_bars: int,
    ) -> None:
        """Bound in-memory paper histories. Backtests do not call this."""
        closed_ids = [pair.id for pair in self.pairs if not pair.long_open and not pair.short_open]
        if len(closed_ids) > max_closed_pairs:
            drop = set(closed_ids[:-max_closed_pairs])
            self.pairs = [pair for pair in self.pairs if pair.id not in drop]
            self.trades = [leg for leg in self.trades if (leg.pair_id or "") not in drop]
        if len(self.events) > max_events:
            self.events = self.events[-max_events:]
        if len(self.trades) > max_trades:
            self.trades = self.trades[-max_trades:]
        if len(self._bars) > max_bars:
            self._bars = self._bars[-max_bars:]

    def _pnl(
        self, is_long: bool, entry: float, exit_px: float, *, qty: float | None = None
    ) -> float:
        delta = (exit_px - entry) if is_long else (entry - exit_px)
        return delta * (self.params.qty if qty is None else qty) * self.params.point_value

    def _pnl_pips(self, is_long: bool, entry: float, exit_px: float) -> float:
        return pips_raw(
            exit_px=exit_px, entry=entry, pip_size=self.params.pip_size, is_long=is_long
        )

    def _weighted_pips(self, raw: float) -> float:
        return pips_weighted(raw, qty=self.params.qty, qty_ref=self.params.qty_ref)

    def _pair_weighted_pips(self, pair: Pair, raw: float) -> float:
        return pips_weighted(raw, qty=pair.qty, qty_ref=self.params.qty_ref)

    def _leg_qty(self, pair: Pair, is_long: bool) -> float:
        stored = pair.long_qty if is_long else pair.short_qty
        return pair.qty if stored is None else stored

    def _leg_weighted_pips(self, pair: Pair, is_long: bool, raw: float) -> float:
        return pips_weighted(raw, qty=self._leg_qty(pair, is_long), qty_ref=self.params.qty_ref)

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
        entry_ts = pair.long_entry_ts if is_long else pair.short_entry_ts
        leg_qty = self._leg_qty(pair, is_long)
        lots = pair.long_entry_lots if is_long else pair.short_entry_lots
        if not lots and leg_qty > 0:
            lots = [EntryLot(entry_ts or pair.entry_ts, leg_qty)]
        schedule = self._cost_schedule(pair.session)
        swap_rate = (
            schedule.swap_long_pips_per_rollover
            if is_long
            else schedule.swap_short_pips_per_rollover
        )
        financing_weighted = sum(
            rollover_units(
                lot.ts,
                as_of,
                timezone=self.params.swap_timezone,
                rollover_time=self.params.swap_rollover_time,
                triple_weekday=self.params.swap_triple_weekday,
            )
            * swap_rate
            * lot.qty
            for lot in lots
        )
        return CostBreakdown(
            execution_pips=(1 + int(exited)) * schedule.execution_pips_per_side,
            financing_pips=(financing_weighted / leg_qty if leg_qty > 0 else 0.0),
        )

    def _cost_accounting(self, mark: float, mark_ts: datetime | None = None) -> CostAccounting:
        totals = CostAccounting()
        pairs_by_id = {pair.id: pair for pair in self.pairs}
        for leg in self.trades:
            pair = pairs_by_id.get(leg.pair_id or "")
            if pair is None:
                continue
            gross_raw = self._closed_leg_pips(leg)
            gross_weighted = (
                leg.gross_pnl_pips
                if leg.gross_pnl_pips is not None
                else pips_weighted(gross_raw, qty=leg.qty, qty_ref=self.params.qty_ref)
            )
            weight = leg.qty / self.params.qty_ref
            s_pips = pair.sl_dist / self.params.pip_size
            totals.gross_realized_pips += gross_weighted
            totals.realized_cost_pips += leg.cost_pips
            totals.execution_cost_pips += leg.execution_cost_pips
            totals.financing_cost_pips += leg.financing_cost_pips
            totals.realized_spread_cost_pips += (
                self._cost_schedule(pair.session).spread_pips_per_side * 2 * weight
            )
            totals.spread_cost_pips += (
                self._cost_schedule(pair.session).spread_pips_per_side * 2 * weight
            )
            if s_pips > 0:
                ratio_weight = leg.qty / pair.qty
                totals.gross_realized_r += (gross_raw / s_pips) * ratio_weight
                totals.realized_cost_r += (leg.cost_pips / weight / s_pips) * ratio_weight
            sides = leg.entry_fills + 1
            totals.transaction_sides += sides
            totals.completed_transaction_sides += sides
            totals.side_equivalents += 2 * weight
            totals.completed_side_equivalents += 2 * weight
        for pair in self.pairs:
            s_pips = pair.sl_dist / self.params.pip_size
            for is_long in (True, False):
                is_open = pair.long_open if is_long else pair.short_open
                if not is_open:
                    continue
                leg_qty = self._leg_qty(pair, is_long)
                weight = leg_qty / self.params.qty_ref
                as_of = mark_ts or (
                    self.last_bar.ts if self.last_bar is not None else pair.entry_ts
                )
                gross_raw = self._pnl_pips(is_long, self._leg_entry(pair, is_long), mark)
                costs = self._leg_cost(pair, is_long=is_long, as_of=as_of, exited=False)
                gross_weighted = gross_raw * weight
                cost_weighted = costs.total_pips * weight
                execution_weighted = costs.execution_pips * weight
                financing_weighted = costs.financing_pips * weight
                entry_fills = pair.long_entry_fills if is_long else pair.short_entry_fills
                schedule = self._cost_schedule(pair.session)
                totals.gross_unrealized_pips += gross_weighted
                totals.unrealized_cost_pips += cost_weighted
                if s_pips > 0:
                    ratio_weight = leg_qty / pair.qty
                    totals.gross_unrealized_r += (gross_raw / s_pips) * ratio_weight
                    totals.unrealized_cost_r += (costs.total_pips / s_pips) * ratio_weight
                totals.execution_cost_pips += execution_weighted
                totals.financing_cost_pips += financing_weighted
                totals.spread_cost_pips += schedule.spread_pips_per_side * weight
                totals.transaction_sides += entry_fills
                totals.side_equivalents += weight
        return totals

    def _performance_view(
        self,
        *,
        accounting: CostAccounting,
        unrealized: float,
        equity_pips: float,
        breakeven_pips_per_side: float | None,
        configured_spread_per_side: float,
        configured_execution_cost_per_side: float,
    ) -> PerformanceView:
        """Restate every additive pip metric in the configured reporting unit."""
        factor = conversion_factor(
            unit=self.params.performance_unit.value,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
        )
        gross_realized = accounting.gross_realized_pips
        net_realized = gross_realized - accounting.realized_cost_pips
        gross_unrealized = accounting.gross_unrealized_pips
        net_unrealized = gross_unrealized - accounting.unrealized_cost_pips
        gross_equity = gross_realized + gross_unrealized
        equity_cost = accounting.realized_cost_pips + accounting.unrealized_cost_pips

        def scaled(value: float | None) -> float | None:
            return None if value is None else value * factor

        return PerformanceView(
            unit=self.params.performance_unit,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
            conversion_factor=factor,
            unit_label="$" if self.params.performance_unit is PerformanceUnit.DOLLARS else "pips",
            realized=net_realized * factor,
            unrealized=net_unrealized * factor,
            equity=equity_pips * factor,
            gross_realized=gross_realized * factor,
            realized_cost=accounting.realized_cost_pips * factor,
            net_realized=net_realized * factor,
            gross_unrealized=gross_unrealized * factor,
            unrealized_cost=accounting.unrealized_cost_pips * factor,
            net_unrealized=net_unrealized * factor,
            gross_equity=gross_equity * factor,
            equity_cost=equity_cost * factor,
            net_equity=(gross_equity - equity_cost) * factor,
            execution_cost=accounting.execution_cost_pips * factor,
            financing_cost=accounting.financing_cost_pips * factor,
            max_drawdown=self.max_drawdown_pips * factor,
            gross_max_drawdown=self.max_drawdown_pips * factor,
            net_max_drawdown=self.net_max_drawdown_pips * factor,
            breakeven_per_completed_side=scaled(breakeven_pips_per_side),
            configured_spread_per_side=configured_spread_per_side * factor,
            configured_execution_cost_per_side=configured_execution_cost_per_side * factor,
        )

    def _pips_to_dollars(self, pips: float) -> float | None:
        return cash(
            pips,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
        )

    def _pair_pips_to_dollars(self, pair: Pair, raw_pips: float) -> float | None:
        return self._pips_to_dollars(self._pair_weighted_pips(pair, raw_pips))

    def _leg_pips_to_dollars(self, pair: Pair, is_long: bool, raw_pips: float) -> float | None:
        return self._pips_to_dollars(self._leg_weighted_pips(pair, is_long, raw_pips))

    def _closed_leg_pips(self, leg: ClosedLeg) -> float:
        if leg.pnl_pips is not None:
            return leg.pnl_pips
        return self._pnl_pips(leg.side == "long", leg.entry, leg.exit)

    def _unrealized_pips(self, mark: float) -> float:
        unrealized = 0.0
        for pair in self.pairs:
            if pair.long_open:
                unrealized += self._leg_weighted_pips(
                    pair, True, self._pnl_pips(True, self._leg_entry(pair, True), mark)
                )
            if pair.short_open:
                unrealized += self._leg_weighted_pips(
                    pair, False, self._pnl_pips(False, self._leg_entry(pair, False), mark)
                )
        return unrealized

    def _record_equity(self, mark: float, bar: Candle | None = None) -> None:
        if bar is not None and self.m1_bars:
            for m1 in m1_covering(bar, self.m1_bars, self.params.timeframe_minutes):
                self._mark_equity(m1.close, m1.ts)
        self._mark_equity(mark, bar.ts if bar is not None else None)

    def _mark_equity(self, mark: float, mark_ts: datetime | None = None) -> None:
        equity_pips = self.stats.realized_pips + self._unrealized_pips(mark)
        self.equity_peak_pips = max(self.equity_peak_pips, equity_pips)
        self.max_drawdown_pips = max(self.max_drawdown_pips, self.equity_peak_pips - equity_pips)
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
        if self._collect_equity_curve and mark_ts is not None:
            self._equity_curve_pips[mark_ts] = (
                net_equity_pips,
                self.net_equity_peak_pips - net_equity_pips,
            )
        if self.prop_guard.enabled and mark_ts is not None:
            assert self.prop_guard.profile is not None
            equity_delta = cash(
                net_equity_pips,
                dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
                qty_ref=self.params.qty_ref,
            )
            assert equity_delta is not None
            equity_cash = self.prop_guard.profile.initial_balance + equity_delta
            if self.prop_guard.evaluate(mark_ts, equity_cash):
                self.events.append(
                    EngineEvent(
                        kind="prop_guard_breached",
                        session="risk",
                        ts=mark_ts,
                        detail={
                            "reason": self.prop_guard.state.breach_reason,
                            "equity_cash": equity_cash,
                        },
                    )
                )

    def _equity_curve(self) -> list[EquityCurvePoint]:
        factor = conversion_factor(
            unit=self.params.performance_unit.value,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
        )
        return [
            EquityCurvePoint(
                ts=ts,
                net_equity=net_equity_pips * factor,
                net_drawdown=net_drawdown_pips * factor,
            )
            for ts, (net_equity_pips, net_drawdown_pips) in sorted(
                self._equity_curve_pips.items()
            )
        ]

    def _record_excursions(self, bar: Candle) -> None:
        for pair in self.pairs:
            if pair.long_open:
                entry = self._leg_entry(pair, True)
                pair.long_mae_pips = min(pair.long_mae_pips, self._pnl_pips(True, entry, bar.low))
                pair.long_mfe_pips = max(pair.long_mfe_pips, self._pnl_pips(True, entry, bar.high))
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
            if self.params.entry_mode is EntryMode.HEDGE_PAIR or (
                self.params.entry_mode is EntryMode.CONTINGENT_HEDGE
                and self.params.hedge_ratio_initial == 1
            ):
                self._open_pair(session, bar.open, signal.range_price, open_ts, signal.bullish)
            elif self.params.entry_mode is EntryMode.OCO_BRACKET:
                assert signal.range_high is not None and signal.range_low is not None
                self._stage_oco_bracket(
                    session=session,
                    entry=bar.open,
                    range_price=signal.range_price,
                    range_high=signal.range_high,
                    range_low=signal.range_low,
                    ts=open_ts,
                    bullish=signal.bullish,
                )
            elif (
                self.params.entry_mode is EntryMode.CONTINGENT_HEDGE
                and 0 < self.params.hedge_ratio_initial < 1
            ):
                self._stage_fractional_contingent(
                    session, bar.open, signal.range_price, open_ts, signal.bullish
                )
            else:
                self._stage_synthetic_order(
                    session, bar.open, signal.range_price, open_ts, signal.bullish
                )

    def _fill_entry_orders(self, bar: Candle) -> None:
        if not self.entry_orders:
            return
        fill_ts = bar_open(bar, self.params.timeframe_minutes)
        for order in list(self.entry_orders):
            hit = self._resolve_oco_trigger(
                bullish_signal=order.bullish,
                bar=bar,
                upper=order.upper_trigger,
                lower=order.lower_trigger,
            )
            if hit.side == "none" or hit.fill is None:
                if order.expiry_bars is not None:
                    order.bars_seen += 1
                    if order.bars_seen >= order.expiry_bars:
                        self.entry_orders.remove(order)
                        self.events.append(
                            EngineEvent(
                                kind="entry_order_cancelled",
                                session=order.session,
                                ts=bar.ts,
                                detail={
                                    "pair_id": order.id,
                                    "reason": "expired",
                                    "bars_seen": order.bars_seen,
                                    "reentry_index": order.reentry_index,
                                },
                            )
                        )
                continue
            self.entry_orders.remove(order)
            self.events.append(
                EngineEvent(
                    kind="entry_order_cancelled",
                    session=order.session,
                    ts=bar.ts,
                    detail={
                        "pair_id": order.id,
                        "reason": "oco_sibling",
                        "cancelled_side": "short" if hit.side == "long" else "long",
                        "reentry_index": order.reentry_index,
                    },
                )
            )
            existing = next((pair for pair in self.pairs if pair.id == order.id), None)
            if order.mode is EntryMode.CONTINGENT_HEDGE and existing is not None:
                self._scale_fractional_contingent(existing, order, hit, bar)
                continue
            is_long = hit.side == "long"
            pair = Pair(
                id=order.id,
                session=order.session,
                entry=hit.fill,
                reference_entry=order.reference_entry,
                sl_dist=order.sl_dist,
                long_sl=(
                    hit.fill - order.sl_dist
                    if order.mode is EntryMode.OCO_BRACKET
                    else (
                        order.reference_entry - order.sl_dist
                        if order.mode is EntryMode.CONTINGENT_HEDGE and not is_long
                        else order.long_sl
                    )
                ),
                long_tp=(
                    hit.fill + order.sl_dist * self._initial_target_r()
                    if order.mode is EntryMode.OCO_BRACKET
                    else order.long_tp
                ),
                short_sl=(
                    hit.fill + order.sl_dist
                    if order.mode is EntryMode.OCO_BRACKET
                    else (
                        order.reference_entry + order.sl_dist
                        if order.mode is EntryMode.CONTINGENT_HEDGE and is_long
                        else order.short_sl
                    )
                ),
                short_tp=(
                    hit.fill - order.sl_dist * self._initial_target_r()
                    if order.mode is EntryMode.OCO_BRACKET
                    else order.short_tp
                ),
                qty=order.qty,
                long_qty=order.qty if is_long else 0.0,
                short_qty=order.qty if not is_long else 0.0,
                long_entry_fills=1 if is_long else 0,
                short_entry_fills=1 if not is_long else 0,
                initial_risk_pct=order.initial_risk_pct,
                initial_risk_cash=order.initial_risk_cash,
                primary_side=hit.side,
                long_open=is_long,
                short_open=not is_long,
                locked=True,
                entry_ts=fill_ts,
                long_entry=hit.fill if is_long else None,
                short_entry=hit.fill if not is_long else None,
                long_entry_ts=fill_ts if is_long else None,
                short_entry_ts=fill_ts if not is_long else None,
                long_entry_lots=[EntryLot(fill_ts, order.qty)] if is_long else [],
                short_entry_lots=[EntryLot(fill_ts, order.qty)] if not is_long else [],
                entry_gap=hit.gap,
                entry_ambiguous=hit.ambiguous,
                entry_bar_close_ts=bar.ts,
                entry_m1_index=hit.child_index,
                contingent_initial_ratio=(
                    0.0 if order.mode is EntryMode.CONTINGENT_HEDGE else None
                ),
                hedge_failure_threshold=self._failure_threshold(
                    order.reference_entry, order.sl_dist, is_long
                )
                if order.mode is EntryMode.CONTINGENT_HEDGE
                else None,
                hedge_ratio_staged=(
                    self.params.hedge_ratio_staged
                    if order.mode is EntryMode.CONTINGENT_HEDGE
                    else 0.0
                ),
                entry_mode=order.mode,
                reentry_index=order.reentry_index,
                root_id=order.root_id or order.id,
                bracket_upper=order.upper_trigger,
                bracket_lower=order.lower_trigger,
                bullish_signal=order.bullish,
            )
            self.pairs.append(pair)
            self.events.append(
                EngineEvent(
                    kind="entry",
                    session=pair.session,
                    ts=fill_ts,
                    detail={
                        "entry": hit.fill,
                        "reference_entry": order.reference_entry,
                        "sl_dist": pair.sl_dist,
                        "sl_pips": pair.sl_dist / self.params.pip_size,
                        "bullish_signal": order.bullish,
                        "primary_side": pair.primary_side,
                        "pair_id": pair.id,
                        "qty": pair.qty,
                        "initial_risk_pct": pair.initial_risk_pct,
                        "initial_risk_cash": pair.initial_risk_cash,
                        "entry_mode": order.mode.value,
                        "gap_fill": hit.gap,
                        "same_bar_ambiguous": hit.ambiguous,
                    },
                )
            )

    def _stop_distance(self, range_price: float, session: str) -> float | None:
        if self.params.stop_mode in {StopMode.ATR14, StopMode.ORB_ATR14_BLEND}:
            atr = wilder_atr(self._bars, ATR14_PERIOD)
            if atr is None:
                return None
        else:
            atr = None
        if self.params.stop_mode == StopMode.FIXED_PIPS:
            base = self.params.fixed_stop_pips * self.params.pip_size
        elif self.params.stop_mode == StopMode.ATR14:
            assert atr is not None
            base = atr * self.params.sl_mult
        elif self.params.stop_mode == StopMode.ORB_ATR14_BLEND:
            assert atr is not None
            base = blended_orb_atr(range_price, atr) * self.params.sl_mult
        else:
            base = range_price * self.params.sl_mult
        floor_pips = effective_min_stop_pips(
            min_stop_pips=self.params.min_stop_pips,
            min_stop_cost_mult=self.params.min_stop_cost_mult,
            schedule=self._cost_schedule(session),
        )
        return max(base, floor_pips * self.params.pip_size)

    def _sized_stop(self, range_price: float, session: str, ts: datetime) -> float | None:
        sl_dist = self._stop_distance(range_price, session)
        if sl_dist is None:
            self._suppress_signal(session=session, ts=ts, reason="insufficient_atr")
            return None
        if sl_dist <= 0:
            self.non_positive_stop_count += 1
            self.events.append(
                EngineEvent(
                    kind="signal_skipped_non_positive_stop",
                    session=session,
                    ts=ts,
                    detail={"sl_dist": sl_dist, "range_price": range_price},
                )
            )
            return None
        return sl_dist

    def _initial_target_r(self) -> float:
        return initial_target_r(
            tp_mode=self.params.tp_mode,
            rr=self.params.rr,
            partial_tp_r=self.params.partial_tp_r,
        )

    def _equity_cash(self, mark: float, ts: datetime) -> float | None:
        if self.params.dollars_per_pip_per_qty is None:
            return None
        accounting = self._cost_accounting(mark, ts)
        net_pips = (
            accounting.gross_realized_pips
            + accounting.gross_unrealized_pips
            - accounting.realized_cost_pips
            - accounting.unrealized_cost_pips
        )
        delta = cash(
            net_pips,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
            qty_ref=self.params.qty_ref,
        )
        assert delta is not None
        return self.params.initial_capital + delta

    def _sizing_decision(
        self, *, session: str, entry: float, sl_dist: float, ts: datetime
    ) -> SizingDecision:
        s_pips = sl_dist / self.params.pip_size
        slippage = self._cost_schedule(session).slippage_pips_per_side
        equity_cash = self._equity_cash(entry, ts)
        if self.params.risk_mode is RiskMode.FIXED_FRACTIONAL:
            assert equity_cash is not None
            assert self.params.dollars_per_pip_per_qty is not None
            return fixed_fractional_size(
                equity_cash=equity_cash,
                s_pips=s_pips,
                slippage_pips_per_side=slippage,
                dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
                risk_pct_per_r=self.params.risk_pct_per_r,
                max_pair_risk_pct=self.params.max_pair_risk_pct,
            )
        return fixed_qty_size(
            qty=self.params.qty,
            equity_cash=equity_cash,
            s_pips=s_pips,
            slippage_pips_per_side=slippage,
            dollars_per_pip_per_qty=self.params.dollars_per_pip_per_qty,
        )

    def _suppress_signal(self, *, session: str, ts: datetime, reason: str) -> None:
        self.suppressed_signal_count += 1
        self.suppressed_signal_reasons[reason] = self.suppressed_signal_reasons.get(reason, 0) + 1
        self.events.append(
            EngineEvent(
                kind="signal_suppressed_risk",
                session=session,
                ts=ts,
                detail={"reason": reason},
            )
        )

    def _skip_filter(self, *, session: str, ts: datetime, reason: str) -> None:
        self.trades_skipped_by_filter += 1
        self.suppressed_signal_count += 1
        self.suppressed_signal_reasons[reason] = self.suppressed_signal_reasons.get(reason, 0) + 1
        self.events.append(
            EngineEvent(
                kind="signal_skipped_filter",
                session=session,
                ts=ts,
                detail={"reason": reason},
            )
        )

    def _filter_blocks(self, session: str, range_price: float, ts: datetime, bullish: bool) -> bool:
        reason = entry_filter_reason(
            filter_d1_ema50=self.params.filter_d1_ema50,
            filter_nr7=self.params.filter_nr7,
            filter_orb_atr_min=self.params.filter_orb_atr_min,
            filter_orb_atr_max=self.params.filter_orb_atr_max,
            bullish=bullish,
            range_price=range_price,
            session_orb_ranges=self._orb_ranges.get(session, []),
            prior_d1=self._d1.prior_close_and_ema(),
            atr=wilder_atr(self._bars, ATR14_PERIOD),
        )
        if reason is None:
            return False
        self._skip_filter(session=session, ts=ts, reason=reason)
        return True

    def _open_risk_pct(self, equity_cash: float | None) -> float:
        open_pairs = [pair for pair in self.pairs if pair.long_open or pair.short_open]
        risk_cash = {pair.id: pair.initial_risk_cash or 0.0 for pair in open_pairs}
        risk_pct = {pair.id: pair.initial_risk_pct or 0.0 for pair in open_pairs}
        for order in self.entry_orders:
            risk_cash.setdefault(order.id, order.initial_risk_cash or 0.0)
            risk_pct.setdefault(order.id, order.initial_risk_pct or 0.0)
        if equity_cash is not None and equity_cash > 0:
            cash_total = sum(risk_cash.values())
            if cash_total > 0:
                return 100.0 * cash_total / equity_cash
        return sum(risk_pct.values())

    def _accept_structure(
        self, *, session: str, entry: float, sl_dist: float, ts: datetime
    ) -> SizingDecision | None:
        if self.prop_guard.blocks_new:
            self._suppress_signal(session=session, ts=ts, reason="prop_guard")
            return None
        active_pairs = [pair for pair in self.pairs if pair.long_open or pair.short_open]
        active_sessions = {pair.session for pair in active_pairs} | {
            order.session for order in self.entry_orders
        }
        active_count = len({pair.id for pair in active_pairs} | {o.id for o in self.entry_orders})
        if self.params.one_open_per_session and session in active_sessions:
            self._suppress_signal(session=session, ts=ts, reason="one_open_per_session")
            return None
        if (
            self.params.max_concurrent_structures > 0
            and active_count >= self.params.max_concurrent_structures
        ):
            self._suppress_signal(session=session, ts=ts, reason="max_concurrent_structures")
            return None
        decision = self._sizing_decision(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
        equity_cash = self._equity_cash(entry, ts)
        if (
            self.params.risk_mode is RiskMode.FIXED_FRACTIONAL
            and decision.pair_risk_pct is not None
            and decision.pair_risk_pct > self.params.max_pair_risk_pct + 1e-12
        ):
            self._suppress_signal(session=session, ts=ts, reason="max_pair_risk_pct")
            return None
        if (
            self.params.risk_mode is RiskMode.FIXED_FRACTIONAL
            and self.params.max_open_risk_pct > 0
            and decision.pair_risk_pct is not None
            and self._open_risk_pct(equity_cash) + decision.pair_risk_pct
            > self.params.max_open_risk_pct + 1e-12
        ):
            self._suppress_signal(session=session, ts=ts, reason="max_open_risk_pct")
            return None
        return decision

    def _stage_synthetic_order(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> bool:
        if self._filter_blocks(session, range_price, ts, bullish):
            return False
        sl_dist = self._sized_stop(range_price, session, ts)
        if sl_dist is None:
            return False
        decision = self._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
        if decision is None:
            return False
        plan = synthetic_order_plan(
            entry=entry,
            sl_dist=sl_dist,
            rr=self.params.rr,
            lock_dist=self._plan_lock_offset(sl_dist),
            tp_r=self._initial_target_r(),
        )
        order = EntryOrder(
            id=f"{session}:{ts.isoformat()}",
            session=session,
            mode=self.params.entry_mode,
            reference_entry=entry,
            sl_dist=sl_dist,
            upper_trigger=plan.upper_trigger,
            lower_trigger=plan.lower_trigger,
            bullish=bullish,
            staged_ts=ts,
            qty=decision.qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            long_sl=plan.long_sl,
            long_tp=plan.long_tp,
            short_sl=plan.short_sl,
            short_tp=plan.short_tp,
        )
        self.entry_orders.append(order)
        self.events.append(
            EngineEvent(
                kind="entry_order_staged",
                session=session,
                ts=ts,
                detail={
                    "entry_mode": self.params.entry_mode.value,
                    "pair_id": order.id,
                    "reference_entry": entry,
                    "upper_trigger": plan.upper_trigger,
                    "lower_trigger": plan.lower_trigger,
                    "sl_dist": sl_dist,
                    "qty": decision.qty,
                },
            )
        )
        return True

    def _stage_oco_bracket(
        self,
        *,
        session: str,
        entry: float,
        range_price: float,
        range_high: float,
        range_low: float,
        ts: datetime,
        bullish: bool,
    ) -> bool:
        if self._filter_blocks(session, range_price, ts, bullish):
            return False
        sl_dist = self._sized_stop(range_price, session, ts)
        if sl_dist is None:
            return False
        decision = self._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
        if decision is None:
            return False
        buffer_price = (
            self.params.oco_buffer_value * range_price
            if self.params.oco_buffer_mode is OcoBufferMode.ORB_FRAC
            else self.params.oco_buffer_value * self.params.pip_size
        )
        order = EntryOrder(
            id=f"{session}:{ts.isoformat()}",
            session=session,
            mode=EntryMode.OCO_BRACKET,
            reference_entry=entry,
            sl_dist=sl_dist,
            upper_trigger=range_high + buffer_price,
            lower_trigger=range_low - buffer_price,
            bullish=bullish,
            staged_ts=ts,
            qty=decision.qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            long_sl=0.0,
            long_tp=0.0,
            short_sl=0.0,
            short_tp=0.0,
            expiry_bars=self.params.oco_expiry_bars,
            root_id=f"{session}:{ts.isoformat()}",
        )
        self.entry_orders.append(order)
        self.events.append(
            EngineEvent(
                kind="entry_order_staged",
                session=session,
                ts=ts,
                detail={
                    "entry_mode": EntryMode.OCO_BRACKET.value,
                    "pair_id": order.id,
                    "upper_trigger": order.upper_trigger,
                    "lower_trigger": order.lower_trigger,
                    "buffer": buffer_price,
                    "expiry_bars": order.expiry_bars,
                    "reentry_index": 0,
                    "qty": order.qty,
                },
            )
        )
        return True

    def _failure_threshold(self, entry: float, sl_dist: float, is_long: bool) -> float:
        offset = sl_dist - self.params.hedge_failure_k * sl_dist
        return entry + offset if is_long else entry - offset

    def _stage_fractional_contingent(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> bool:
        if self._filter_blocks(session, range_price, ts, bullish):
            return False
        sl_dist = self._sized_stop(range_price, session, ts)
        if sl_dist is None:
            return False
        decision = self._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
        if decision is None:
            return False
        ratio = self.params.hedge_ratio_initial
        initial_qty = decision.qty * ratio
        plan = hedge_pair_plan(
            entry=entry, sl_dist=sl_dist, rr=self.params.rr, tp_r=self._initial_target_r()
        )
        pair = Pair(
            id=f"{session}:{ts.isoformat()}",
            session=session,
            entry=entry,
            reference_entry=entry,
            sl_dist=sl_dist,
            long_sl=plan.long_sl,
            long_tp=plan.long_tp,
            short_sl=plan.short_sl,
            short_tp=plan.short_tp,
            qty=decision.qty,
            long_qty=initial_qty,
            short_qty=initial_qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            primary_side=None,
            entry_ts=ts,
            long_entry=entry,
            short_entry=entry,
            long_entry_ts=ts,
            short_entry_ts=ts,
            long_entry_lots=[EntryLot(ts, initial_qty)],
            short_entry_lots=[EntryLot(ts, initial_qty)],
            contingent_initial_ratio=ratio,
            hedge_ratio_staged=self.params.hedge_ratio_staged,
            entry_mode=EntryMode.CONTINGENT_HEDGE,
            bullish_signal=bullish,
        )
        synthetic = synthetic_order_plan(
            entry=entry,
            sl_dist=sl_dist,
            rr=self.params.rr,
            lock_dist=self._plan_lock_offset(sl_dist),
            tp_r=self._initial_target_r(),
        )
        order = EntryOrder(
            id=pair.id,
            session=session,
            mode=EntryMode.CONTINGENT_HEDGE,
            reference_entry=entry,
            sl_dist=sl_dist,
            upper_trigger=synthetic.upper_trigger,
            lower_trigger=synthetic.lower_trigger,
            bullish=bullish,
            staged_ts=ts,
            qty=decision.qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            long_sl=synthetic.long_sl,
            long_tp=synthetic.long_tp,
            short_sl=synthetic.short_sl,
            short_tp=synthetic.short_tp,
        )
        self.pairs.append(pair)
        self.entry_orders.append(order)
        self.events.append(
            EngineEvent(
                kind="entry",
                session=session,
                ts=ts,
                detail={
                    "entry": entry,
                    "sl_dist": sl_dist,
                    "bullish_signal": bullish,
                    "primary_side": None,
                    "pair_id": pair.id,
                    "qty": initial_qty,
                    "entry_mode": EntryMode.CONTINGENT_HEDGE.value,
                    "hedge_ratio_initial": ratio,
                },
            )
        )
        return True

    def _scale_fractional_contingent(
        self, pair: Pair, order: EntryOrder, hit: OcoTriggerHit, bar: Candle
    ) -> None:
        assert hit.fill is not None and hit.side != "none"
        fill_ts = bar_open(bar, self.params.timeframe_minutes)
        is_long = hit.side == "long"
        pair.primary_side = hit.side
        if is_long:
            self._close_short(
                pair,
                hit.fill,
                fill_ts,
                reason="contingent_initial_stop",
                gap_fill=hit.gap,
            )
        else:
            self._close_long(
                pair,
                hit.fill,
                fill_ts,
                reason="contingent_initial_stop",
                gap_fill=hit.gap,
            )
        current_qty = self._leg_qty(pair, is_long)
        added_qty = max(0.0, pair.qty - current_qty)
        current_entry = self._leg_entry(pair, is_long)
        average = (
            (current_entry * current_qty + hit.fill * added_qty) / pair.qty
            if pair.qty > 0
            else hit.fill
        )
        if is_long:
            pair.long_qty = pair.qty
            pair.long_entry = average
            pair.long_entry_fills += int(added_qty > 0)
            if added_qty > 0:
                pair.long_entry_lots.append(EntryLot(fill_ts, added_qty))
            pair.long_sl = order.long_sl
            pair.long_tp = order.long_tp
        else:
            pair.short_qty = pair.qty
            pair.short_entry = average
            pair.short_entry_fills += int(added_qty > 0)
            if added_qty > 0:
                pair.short_entry_lots.append(EntryLot(fill_ts, added_qty))
            pair.short_sl = order.short_sl
            pair.short_tp = order.short_tp
        pair.locked = True
        pair.entry_gap = hit.gap
        pair.entry_ambiguous = hit.ambiguous
        pair.entry_bar_close_ts = bar.ts
        pair.entry_m1_index = hit.child_index
        pair.hedge_failure_threshold = self._failure_threshold(
            order.reference_entry, order.sl_dist, is_long
        )
        self.events.append(
            EngineEvent(
                kind="entry",
                session=pair.session,
                ts=fill_ts,
                detail={
                    "entry": hit.fill,
                    "reference_entry": order.reference_entry,
                    "pair_id": pair.id,
                    "primary_side": pair.primary_side,
                    "qty": added_qty,
                    "entry_mode": EntryMode.CONTINGENT_HEDGE.value,
                    "hedge_ratio_initial": pair.contingent_initial_ratio,
                    "gap_fill": hit.gap,
                },
            )
        )

    def _stage_contingent_hedges(self, bar: Candle) -> None:
        fill_ts = bar_open(bar, self.params.timeframe_minutes)
        for pair in self.pairs:
            threshold = pair.hedge_failure_threshold
            if (
                pair.contingent_initial_ratio is None
                or pair.primary_side is None
                or pair.hedge_staged
                or threshold is None
                or pair.hedge_ratio_staged <= 0
            ):
                continue
            if (
                pair.entry_bar_close_ts == bar.ts
                and self.params.intrabar_mode is IntrabarMode.OPTIMISTIC
            ):
                continue
            long_primary = pair.primary_side == "long"
            touched = bar.low <= threshold if long_primary else bar.high >= threshold
            if not touched:
                continue
            desired_qty = pair.qty * pair.hedge_ratio_staged
            hedge_is_long = not long_primary
            hedge_open = pair.long_open if hedge_is_long else pair.short_open
            current_qty = self._leg_qty(pair, hedge_is_long) if hedge_open else 0.0
            added_qty = max(0.0, desired_qty - current_qty)
            if added_qty <= 0:
                pair.hedge_staged = True
                continue
            if long_primary:
                fill = bar.open if bar.open <= threshold else threshold
                pair.short_open = True
                pair.short_episode += int(not hedge_open)
                pair.short_entry = fill
                pair.short_entry_ts = fill_ts
                pair.short_qty = desired_qty
                pair.short_entry_fills = 1
                pair.short_entry_lots = [EntryLot(fill_ts, desired_qty)]
            else:
                fill = bar.open if bar.open >= threshold else threshold
                pair.long_open = True
                pair.long_episode += int(not hedge_open)
                pair.long_entry = fill
                pair.long_entry_ts = fill_ts
                pair.long_qty = desired_qty
                pair.long_entry_fills = 1
                pair.long_entry_lots = [EntryLot(fill_ts, desired_qty)]
            pair.hedge_staged = True
            self.events.append(
                EngineEvent(
                    kind="hedge_staged",
                    session=pair.session,
                    ts=fill_ts,
                    detail={
                        "pair_id": pair.id,
                        "side": "long" if hedge_is_long else "short",
                        "fill": fill,
                        "failure_threshold": threshold,
                        "qty": added_qty,
                        "hedge_ratio_staged": pair.hedge_ratio_staged,
                    },
                )
            )

    def _stage_oco_reentries(self, bar: Candle) -> None:
        if not self.params.allow_reentry:
            return
        for pair in self.pairs:
            if (
                pair.entry_mode is not EntryMode.OCO_BRACKET
                or pair.long_open
                or pair.short_open
                or pair.reentry_index != 0
                or pair.reentry_staged
                or pair.bracket_upper is None
                or pair.bracket_lower is None
            ):
                continue
            pair.reentry_staged = True
            reference = pair.reference_entry if pair.reference_entry is not None else pair.entry
            decision = self._accept_structure(
                session=pair.session,
                entry=reference,
                sl_dist=pair.sl_dist,
                ts=bar.ts,
            )
            if decision is None:
                continue
            root_id = pair.root_id or pair.id
            order = EntryOrder(
                id=f"{root_id}:reentry:1",
                session=pair.session,
                mode=EntryMode.OCO_BRACKET,
                reference_entry=reference,
                sl_dist=pair.sl_dist,
                upper_trigger=pair.bracket_upper,
                lower_trigger=pair.bracket_lower,
                bullish=pair.bullish_signal,
                staged_ts=bar.ts,
                qty=decision.qty,
                initial_risk_pct=decision.pair_risk_pct,
                initial_risk_cash=decision.pair_risk_cash,
                long_sl=0.0,
                long_tp=0.0,
                short_sl=0.0,
                short_tp=0.0,
                expiry_bars=self.params.oco_expiry_bars,
                reentry_index=1,
                root_id=root_id,
            )
            self.entry_orders.append(order)
            self.events.append(
                EngineEvent(
                    kind="entry_order_staged",
                    session=pair.session,
                    ts=bar.ts,
                    detail={
                        "entry_mode": EntryMode.OCO_BRACKET.value,
                        "pair_id": order.id,
                        "upper_trigger": order.upper_trigger,
                        "lower_trigger": order.lower_trigger,
                        "expiry_bars": order.expiry_bars,
                        "reentry_index": 1,
                        "qty": order.qty,
                    },
                )
            )

    def _open_pair(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> bool:
        if self._filter_blocks(session, range_price, ts, bullish):
            return False
        sl_dist = self._sized_stop(range_price, session, ts)
        if sl_dist is None:
            return False
        plan = hedge_pair_plan(
            entry=entry, sl_dist=sl_dist, rr=self.params.rr, tp_r=self._initial_target_r()
        )
        decision = self._accept_structure(session=session, entry=entry, sl_dist=sl_dist, ts=ts)
        if decision is None:
            return False
        pair = Pair(
            id=f"{session}:{ts.isoformat()}",
            session=session,
            entry=plan.reference_entry,
            sl_dist=plan.sl_dist,
            long_sl=plan.long_sl,
            long_tp=plan.long_tp,
            short_sl=plan.short_sl,
            short_tp=plan.short_tp,
            qty=decision.qty,
            long_qty=decision.qty,
            short_qty=decision.qty,
            initial_risk_pct=decision.pair_risk_pct,
            initial_risk_cash=decision.pair_risk_cash,
            primary_side="long" if bullish else "short",
            entry_ts=ts,
            long_open=plan.long_open,
            short_open=plan.short_open,
            long_entry=plan.long_entry,
            short_entry=plan.short_entry,
            long_entry_ts=ts,
            short_entry_ts=ts,
            long_entry_lots=[EntryLot(ts, decision.qty)],
            short_entry_lots=[EntryLot(ts, decision.qty)],
            entry_mode=self.params.entry_mode,
            bullish_signal=bullish,
        )
        self.pairs.append(pair)
        self._emit_entry(pair, ts, bullish_signal=bullish)
        return True

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
                    "qty": pair.qty,
                    "initial_risk_pct": pair.initial_risk_pct,
                    "initial_risk_cash": pair.initial_risk_cash,
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
        ranges = self._orb_ranges.setdefault(collector.session, [])
        ranges.append(bars_range)
        overflow = len(ranges) - NR7_LOOKBACK
        if overflow > 0:
            del ranges[:overflow]
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
            range_high=collector.high,
            range_low=collector.low,
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
                    total += r_multiple(leg.pnl_pips, s_pips=s_pips) * (leg.qty / pair.qty)
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
                total += r_multiple(leg.pnl_pips, s_pips=s_pips) * (leg.qty / pair.qty)
        return total

    def _realized_r(self) -> float:
        by_id = {pair.id: pair for pair in self.pairs}
        total = 0.0
        for leg in self.trades:
            pair = by_id.get(leg.pair_id or "")
            if pair is None or leg.pnl_pips is None or pair.sl_dist <= 0:
                continue
            total += r_multiple(leg.pnl_pips, s_pips=pair.sl_dist / self.params.pip_size) * (
                leg.qty / pair.qty
            )
        return total

    def _unrealized_r(self, mark: float) -> float:
        total = 0.0
        for pair in self.pairs:
            s_pips = pair.sl_dist / self.params.pip_size
            if s_pips == 0:
                continue
            if pair.long_open:
                raw = self._pnl_pips(True, self._leg_entry(pair, True), mark)
                total += r_multiple(raw, s_pips=s_pips) * (self._leg_qty(pair, True) / pair.qty)
            if pair.short_open:
                raw = self._pnl_pips(False, self._leg_entry(pair, False), mark)
                total += r_multiple(raw, s_pips=s_pips) * (self._leg_qty(pair, False) / pair.qty)
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
                    time_exit=any(
                        leg is not None and leg.reason == "time_exit"
                        for leg in (long_leg, short_leg)
                    ),
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
            due = time_exit_due(
                entry_ts=pair.entry_ts,
                bar_close_ts=bar.ts,
                mode=self.params.time_exit_mode,
                max_age_hours=self.params.max_age_hours,
            )
            if pair.reference_entry is not None and int(pair.long_open) + int(pair.short_open) == 1:
                self._manage_synthetic_leg(pair, bar, due=due)
                continue
            if due and int(pair.long_open) + int(pair.short_open) == 1:
                self._resolve_single_leg_or_time_exit(pair, bar)
                continue
            long_hit_sl = pair.long_open and bar.low <= pair.long_sl
            long_hit_tp = pair.long_open and bar.high >= pair.long_tp
            short_hit_sl = pair.short_open and bar.high >= pair.short_sl
            short_hit_tp = pair.short_open and bar.low <= pair.short_tp

            if not pair.locked and long_hit_sl and short_hit_sl:
                long_fill = _fill_stop(bar.open, pair.long_sl, True)
                short_fill = _fill_stop(bar.open, pair.short_sl, False)
                self._close_long(
                    pair,
                    long_fill,
                    bar.ts,
                    gap_fill=self._level_gap(long_fill, pair.long_sl),
                )
                self._close_short(
                    pair,
                    short_fill,
                    bar.ts,
                    gap_fill=self._level_gap(short_fill, pair.short_sl),
                )
            elif not pair.locked:
                if long_hit_sl:
                    fill = _fill_stop(bar.open, pair.long_sl, True)
                    self._close_long(
                        pair,
                        fill,
                        bar.ts,
                        gap_fill=self._level_gap(fill, pair.long_sl),
                    )
                    if pair.short_open:
                        self._apply_lock(pair, long_survives=False, ts=bar.ts)
                        self._resolve_after_lock(pair, bar, is_long=False)
                elif short_hit_sl:
                    fill = _fill_stop(bar.open, pair.short_sl, False)
                    self._close_short(
                        pair,
                        fill,
                        bar.ts,
                        gap_fill=self._level_gap(fill, pair.short_sl),
                    )
                    if pair.long_open:
                        self._apply_lock(pair, long_survives=True, ts=bar.ts)
                        self._resolve_after_lock(pair, bar, is_long=True)
                elif long_hit_tp:
                    fill = _fill_limit(bar.open, pair.long_tp, True)
                    self._handle_target(
                        pair,
                        bar,
                        is_long=True,
                        px=fill,
                        ts=bar.ts,
                        gap_fill=self._level_gap(fill, pair.long_tp),
                    )
                elif short_hit_tp:
                    fill = _fill_limit(bar.open, pair.short_tp, False)
                    self._handle_target(
                        pair,
                        bar,
                        is_long=False,
                        px=fill,
                        ts=bar.ts,
                        gap_fill=self._level_gap(fill, pair.short_tp),
                    )
            else:
                if pair.long_open:
                    if long_hit_sl:
                        fill = _fill_stop(bar.open, pair.long_sl, True)
                        self._close_long(
                            pair,
                            fill,
                            bar.ts,
                            gap_fill=self._level_gap(fill, pair.long_sl),
                        )
                    elif long_hit_tp:
                        fill = _fill_limit(bar.open, pair.long_tp, True)
                        self._handle_target(
                            pair,
                            bar,
                            is_long=True,
                            px=fill,
                            ts=bar.ts,
                            gap_fill=self._level_gap(fill, pair.long_tp),
                        )
                if pair.short_open:
                    if short_hit_sl:
                        fill = _fill_stop(bar.open, pair.short_sl, False)
                        self._close_short(
                            pair,
                            fill,
                            bar.ts,
                            gap_fill=self._level_gap(fill, pair.short_sl),
                        )
                    elif short_hit_tp:
                        fill = _fill_limit(bar.open, pair.short_tp, False)
                        self._handle_target(
                            pair,
                            bar,
                            is_long=False,
                            px=fill,
                            ts=bar.ts,
                            gap_fill=self._level_gap(fill, pair.short_tp),
                        )
            if due:
                if pair.long_open:
                    self._close_long(pair, bar.close, bar.ts, reason="time_exit")
                if pair.short_open:
                    self._close_short(pair, bar.close, bar.ts, reason="time_exit")

    def _manage_synthetic_leg(self, pair: Pair, bar: Candle, *, due: bool) -> None:
        is_long = pair.long_open
        stop = pair.long_sl if is_long else pair.short_sl
        target = pair.long_tp if is_long else pair.short_tp
        hit_stop = bar.low <= stop if is_long else bar.high >= stop
        hit_target = bar.high >= target if is_long else bar.low <= target
        if pair.entry_bar_close_ts == bar.ts:
            same_bar_fill = self._synthetic_entry_bar_exit(
                pair=pair,
                bar=bar,
                is_long=is_long,
                stop=stop,
                target=target,
            )
            if same_bar_fill is not None:
                fill, kind = same_bar_fill
                pair.same_bar_resolved = True
                level = stop if kind == "stop" else target
                gap_fill = self._level_gap(fill, level)
                if kind == "tp":
                    self._handle_target(
                        pair, bar, is_long=is_long, px=fill, ts=bar.ts, gap_fill=gap_fill
                    )
                elif is_long:
                    self._close_long(pair, fill, bar.ts, gap_fill=gap_fill)
                else:
                    self._close_short(pair, fill, bar.ts, gap_fill=gap_fill)
            return
        hit = self._resolve_bar_levels(
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=target,
        )
        if hit.kind != "none" and hit.fill is not None:
            if hit_stop and hit_target or pair.entry_bar_close_ts == bar.ts:
                pair.same_bar_resolved = True
            level = stop if hit.kind == "stop" else target
            gap_fill = self._level_gap(hit.fill, level)
            if hit.kind == "tp":
                self._handle_target(
                    pair, bar, is_long=is_long, px=hit.fill, ts=bar.ts, gap_fill=gap_fill
                )
            elif is_long:
                self._close_long(pair, hit.fill, bar.ts, gap_fill=gap_fill)
            else:
                self._close_short(pair, hit.fill, bar.ts, gap_fill=gap_fill)
            return
        if due:
            if is_long:
                self._close_long(pair, bar.close, bar.ts, reason="time_exit")
            else:
                self._close_short(pair, bar.close, bar.ts, reason="time_exit")

    def _synthetic_entry_bar_exit(
        self, *, pair: Pair, bar: Candle, is_long: bool, stop: float, target: float
    ) -> tuple[float, Literal["stop", "tp"]] | None:
        hit_stop = bar.low <= stop if is_long else bar.high >= stop
        hit_target = bar.high >= target if is_long else bar.low <= target
        covering = m1_covering(bar, self.m1_bars, self.params.timeframe_minutes)
        self._note_resolver_coverage(bar)
        if (
            self.params.intrabar_mode in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE}
            and covering
            and pair.entry_m1_index is not None
        ):
            conservative = self.params.intrabar_mode is IntrabarMode.M1_CONSERVATIVE
            for offset, child in enumerate(covering[pair.entry_m1_index :]):
                child_stop = child.low <= stop if is_long else child.high >= stop
                child_target = child.high >= target if is_long else child.low <= target
                if offset == 0:
                    if child_target and (not child_stop or not conservative):
                        return target, "tp"
                    if child_stop and conservative:
                        return stop, "stop"
                    continue
                if child_stop and child_target:
                    if conservative:
                        return _fill_stop(child.open, stop, is_long), "stop"
                    return _fill_limit(child.open, target, is_long), "tp"
                if child_stop:
                    return _fill_stop(child.open, stop, is_long), "stop"
                if child_target:
                    return _fill_limit(child.open, target, is_long), "tp"
            return None
        if self.params.intrabar_mode is IntrabarMode.OPTIMISTIC:
            return (target, "tp") if hit_target else None
        if hit_stop:
            return stop, "stop"
        if hit_target:
            return target, "tp"
        return None

    def _resolve_single_leg_or_time_exit(self, pair: Pair, bar: Candle) -> None:
        is_long = pair.long_open
        hit = self._resolve_bar_levels(
            is_long=is_long,
            bar=bar,
            stop=pair.long_sl if is_long else pair.short_sl,
            tp=pair.long_tp if is_long else pair.short_tp,
        )
        if hit.kind != "none" and hit.fill is not None:
            level = pair.long_sl if is_long else pair.short_sl
            if hit.kind == "tp":
                level = pair.long_tp if is_long else pair.short_tp
            gap_fill = self._level_gap(hit.fill, level)
            if hit.kind == "tp":
                self._handle_target(
                    pair, bar, is_long=is_long, px=hit.fill, ts=bar.ts, gap_fill=gap_fill
                )
            elif is_long:
                self._close_long(pair, hit.fill, bar.ts, gap_fill=gap_fill)
            else:
                self._close_short(pair, hit.fill, bar.ts, gap_fill=gap_fill)
            return
        if is_long:
            self._close_long(pair, bar.close, bar.ts, reason="time_exit")
        else:
            self._close_short(pair, bar.close, bar.ts, reason="time_exit")

    def _resolve_after_lock(self, pair: Pair, bar: Candle, *, is_long: bool) -> None:
        stop = pair.long_sl if is_long else pair.short_sl
        tp = pair.long_tp if is_long else pair.short_tp
        hit = self._after_lock_same_bar(
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=tp,
        )
        if hit.kind == "none" or hit.fill is None:
            return
        level = stop if hit.kind == "stop" else tp
        gap_fill = self._level_gap(hit.fill, level)
        if hit.kind == "tp":
            self._handle_target(
                pair, bar, is_long=is_long, px=hit.fill, ts=bar.ts, gap_fill=gap_fill
            )
            return
        if is_long:
            self._close_long(pair, hit.fill, bar.ts, gap_fill=gap_fill)
        else:
            self._close_short(pair, hit.fill, bar.ts, gap_fill=gap_fill)

    def _plan_lock_offset(self, sl_dist: float) -> float:
        """Lock offset from the reference entry used by synthetic/contingent plans."""
        if self.params.lock_mode is LockMode.ABSOLUTE:
            return self.lock_dist
        if self.params.lock_mode is LockMode.R_RELATIVE:
            return self.params.lock_r * sl_dist
        return 0.0

    def _lock_stop_price(self, pair: Pair, *, long_survives: bool) -> float:
        if self.params.lock_mode is LockMode.NONE:
            return pair.long_sl if long_survives else pair.short_sl
        if self.params.lock_mode is LockMode.BREAKEVEN:
            return pair.entry
        if self.params.lock_mode is LockMode.R_RELATIVE:
            dist = self.params.lock_r * pair.sl_dist
            return pair.entry + dist if long_survives else pair.entry - dist
        if pair.sl_dist >= self.lock_dist and self.lock_dist > 0:
            return pair.entry + self.lock_dist if long_survives else pair.entry - self.lock_dist
        return pair.entry

    def _apply_lock(self, pair: Pair, *, long_survives: bool, ts: datetime) -> None:
        new_sl = self._lock_stop_price(pair, long_survives=long_survives)
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

    def _level_gap(self, fill: float, level: float) -> bool:
        return abs(fill - level) > self.be_eps

    def _partial_taken(self, pair: Pair, is_long: bool) -> bool:
        return pair.long_partial_taken if is_long else pair.short_partial_taken

    def _handle_target(
        self,
        pair: Pair,
        bar: Candle,
        *,
        is_long: bool,
        px: float,
        ts: datetime,
        gap_fill: bool,
    ) -> None:
        if self.params.tp_mode is TargetMode.PARTIAL_TRAIL and not self._partial_taken(
            pair, is_long
        ):
            self._take_partial(pair, is_long=is_long, px=px, ts=ts, gap_fill=gap_fill)
            self._resolve_after_partial(pair, bar, is_long=is_long)
            return
        if is_long:
            self._close_long(pair, px, ts, gap_fill=gap_fill)
        else:
            self._close_short(pair, px, ts, gap_fill=gap_fill)

    def _take_partial(
        self,
        pair: Pair,
        *,
        is_long: bool,
        px: float,
        ts: datetime,
        gap_fill: bool,
    ) -> None:
        remaining = self._leg_qty(pair, is_long)
        close_qty = remaining * self.params.partial_fraction
        leftover = remaining - close_qty
        if close_qty <= 0 or leftover <= 0:
            if is_long:
                self._close_long(pair, px, ts, gap_fill=gap_fill)
            else:
                self._close_short(pair, px, ts, gap_fill=gap_fill)
            return
        self._record_close(
            pair,
            is_long=is_long,
            px=px,
            ts=ts,
            reason="partial_tp",
            gap_fill=gap_fill,
            qty=close_qty,
        )
        entry = self._leg_entry(pair, is_long)
        runner = target_price(
            entry=entry, sl_dist=pair.sl_dist, target_r=self.params.rr, is_long=is_long
        )
        if is_long:
            pair.long_qty = leftover
            pair.long_partial_taken = True
            pair.long_sl = entry
            pair.long_tp = runner
            pair.long_entry_lots = [
                EntryLot(ts, lot.qty * leftover / remaining) for lot in pair.long_entry_lots
            ]
        else:
            pair.short_qty = leftover
            pair.short_partial_taken = True
            pair.short_sl = entry
            pair.short_tp = runner
            pair.short_entry_lots = [
                EntryLot(ts, lot.qty * leftover / remaining) for lot in pair.short_entry_lots
            ]
        self.events.append(
            EngineEvent(
                kind="partial_tp",
                session=pair.session,
                ts=ts,
                detail={
                    "side": "long" if is_long else "short",
                    "fill": px,
                    "closed_qty": close_qty,
                    "remaining_qty": leftover,
                    "new_sl": entry,
                    "runner_tp": runner,
                },
            )
        )

    def _resolve_after_partial(self, pair: Pair, bar: Candle, *, is_long: bool) -> None:
        if is_long and not pair.long_open:
            return
        if not is_long and not pair.short_open:
            return
        stop = pair.long_sl if is_long else pair.short_sl
        tp = pair.long_tp if is_long else pair.short_tp
        hit = self._resolve_bar_levels(
            is_long=is_long,
            bar=bar,
            stop=stop,
            tp=tp,
        )
        if hit.kind == "none" or hit.fill is None:
            return
        level = stop if hit.kind == "stop" else tp
        gap_fill = self._level_gap(hit.fill, level)
        if hit.kind == "tp":
            self._handle_target(
                pair, bar, is_long=is_long, px=hit.fill, ts=bar.ts, gap_fill=gap_fill
            )
            return
        if is_long:
            self._close_long(pair, hit.fill, bar.ts, gap_fill=gap_fill)
        else:
            self._close_short(pair, hit.fill, bar.ts, gap_fill=gap_fill)

    def _close_long(
        self,
        pair: Pair,
        px: float,
        ts: datetime,
        reason: str = "sl_or_tp",
        *,
        gap_fill: bool = False,
    ) -> None:
        if not pair.long_open:
            return
        self._record_close(pair, is_long=True, px=px, ts=ts, reason=reason, gap_fill=gap_fill)
        pair.long_open = False

    def _close_short(
        self,
        pair: Pair,
        px: float,
        ts: datetime,
        reason: str = "sl_or_tp",
        *,
        gap_fill: bool = False,
    ) -> None:
        if not pair.short_open:
            return
        self._record_close(pair, is_long=False, px=px, ts=ts, reason=reason, gap_fill=gap_fill)
        pair.short_open = False

    def _record_close(
        self,
        pair: Pair,
        *,
        is_long: bool,
        px: float,
        ts: datetime,
        reason: str,
        gap_fill: bool,
        qty: float | None = None,
    ) -> None:
        side: Literal["long", "short"] = "long" if is_long else "short"
        entry = self._leg_entry(pair, is_long)
        leg_qty = self._leg_qty(pair, is_long) if qty is None else qty
        bucket = self._bucket(is_long, entry, px)
        pnl = self._pnl(is_long, entry, px, qty=leg_qty)
        pnl_pips = self._pnl_pips(is_long, entry, px)
        pnl_dollars = self._leg_pips_to_dollars(pair, is_long, pnl_pips)
        costs = self._leg_cost(pair, is_long=is_long, as_of=ts, exited=True)
        weighted_cost = costs.total_pips * (leg_qty / self.params.qty_ref)
        weighted_execution_cost = costs.execution_pips * (leg_qty / self.params.qty_ref)
        weighted_financing_cost = costs.financing_pips * (leg_qty / self.params.qty_ref)
        weighted_gross = pips_weighted(pnl_pips, qty=leg_qty, qty_ref=self.params.qty_ref)
        mae_pips = pair.long_mae_pips if is_long else pair.short_mae_pips
        mfe_pips = pair.long_mfe_pips if is_long else pair.short_mfe_pips
        role: Literal["primary", "hedge", "unknown"]
        if pair.primary_side is None:
            role = "unknown"
        else:
            role = "primary" if side == pair.primary_side else "hedge"
        self.stats.realized += pnl
        self.stats.realized_pips += weighted_gross
        pair.exit_gap = pair.exit_gap or gap_fill
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
                entry_ts=(pair.long_entry_ts if is_long else pair.short_entry_ts) or pair.entry_ts,
                pnl_pips=pnl_pips,
                pnl_dollars=pnl_dollars,
                gross_pnl_pips=weighted_gross,
                cost_pips=weighted_cost,
                net_pnl_pips=weighted_gross - weighted_cost,
                qty=leg_qty,
                episode=pair.long_episode if is_long else pair.short_episode,
                entry_fills=(pair.long_entry_fills if is_long else pair.short_entry_fills),
                execution_cost_pips=weighted_execution_cost,
                financing_cost_pips=weighted_financing_cost,
                reentry_index=pair.reentry_index,
                gap_fill=gap_fill,
                mae_pips=mae_pips,
                mfe_pips=mfe_pips,
                mae_dollars=self._leg_pips_to_dollars(pair, is_long, mae_pips),
                mfe_dollars=self._leg_pips_to_dollars(pair, is_long, mfe_pips),
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
                    "mae_dollars": self._leg_pips_to_dollars(pair, is_long, mae_pips),
                    "mfe_dollars": self._leg_pips_to_dollars(pair, is_long, mfe_pips),
                    "pair_id": pair.id,
                    "role": role,
                    "reason": reason,
                },
            )
        )

    def _trade_pair_results(self, mark: float) -> list[TradePairResult]:
        closed_by_pair_side: dict[tuple[str, str], list[ClosedLeg]] = {}
        for leg in self.trades:
            if leg.pair_id is not None:
                closed_by_pair_side.setdefault((leg.pair_id, leg.side), []).append(leg)
        results: list[TradePairResult] = []
        for pair in self.pairs:
            long_closed = closed_by_pair_side.get((pair.id, "long"), [])
            short_closed = closed_by_pair_side.get((pair.id, "short"), [])
            long_leg = self._pair_leg_result(
                pair,
                True,
                mark,
                None if pair.long_open else (long_closed[-1] if long_closed else None),
            )
            short_leg = self._pair_leg_result(
                pair,
                False,
                mark,
                None if pair.short_open else (short_closed[-1] if short_closed else None),
            )
            prior_closed = (long_closed if pair.long_open else long_closed[:-1]) + (
                short_closed if pair.short_open else short_closed[:-1]
            )
            prior_legs = [
                self._pair_leg_result(pair, leg.side == "long", mark, leg) for leg in prior_closed
            ]
            open_count = int(pair.long_open) + int(pair.short_open)
            status: Literal["open", "partial", "closed"]
            if open_count == 2:
                status = "open"
            elif open_count == 1:
                status = "partial"
            else:
                status = "closed"
            legs = [long_leg, short_leg, *prior_legs]
            pnl_pips = sum(leg.pnl_pips for leg in legs)
            gross_pnl_pips = sum(
                leg.gross_pnl_pips if leg.gross_pnl_pips is not None else leg.pnl_pips
                for leg in legs
            )
            cost_pips = sum(leg.cost_pips for leg in legs)
            stop_pips = pair.sl_dist / self.params.pip_size if pair.sl_dist > 0 else None
            gross_r: float | None = None
            cost_r: float | None = None
            net_r: float | None = None
            hold_hours: float | None = None
            if status == "closed" and stop_pips:
                gross_r = self._pair_r(pair)
                cost_r = cost_pips * self.params.qty_ref / (stop_pips * pair.qty)
                net_r = gross_r - cost_r
                exit_times = [leg.exit_ts for leg in legs if leg.exit_ts is not None]
                if exit_times:
                    hold_hours = (max(exit_times) - pair.entry_ts).total_seconds() / 3600.0
            anchor = self.anchors_by_name.get(pair.session)
            entry_local = pair.entry_ts.astimezone(anchor.tz) if anchor else pair.entry_ts
            if pair.primary_side == "long":
                primary, hedge, unknown = long_leg, short_leg, prior_legs
            elif pair.primary_side == "short":
                primary, hedge, unknown = short_leg, long_leg, prior_legs
            else:
                primary, hedge, unknown = None, None, legs
            results.append(
                TradePairResult(
                    id=pair.id,
                    session=pair.session,
                    entry=pair.entry,
                    entry_ts=pair.entry_ts,
                    qty=pair.qty,
                    initial_risk_pct=pair.initial_risk_pct,
                    initial_risk_cash=pair.initial_risk_cash,
                    status=status,
                    primary=primary,
                    hedge=hedge,
                    unknown_legs=unknown,
                    pnl_pips=pnl_pips,
                    pnl_dollars=self._pips_to_dollars(gross_pnl_pips),
                    gross_pnl_pips=gross_pnl_pips,
                    cost_pips=cost_pips,
                    net_pnl_pips=gross_pnl_pips - cost_pips,
                    entry_mode=pair.entry_mode,
                    reentry_index=pair.reentry_index,
                    entry_gap=pair.entry_gap,
                    exit_gap=pair.exit_gap,
                    same_bar_resolved=pair.same_bar_resolved,
                    stop_pips=stop_pips,
                    gross_r=gross_r,
                    cost_r=cost_r,
                    net_r=net_r,
                    hold_hours=hold_hours,
                    weekday=entry_local.strftime("%A").lower(),
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
            gross_weighted = (
                closed.gross_pnl_pips
                if closed.gross_pnl_pips is not None
                else pips_weighted(pnl_pips, qty=closed.qty, qty_ref=self.params.qty_ref)
            )
            cost_weighted = closed.cost_pips
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
                pnl_dollars=self._pips_to_dollars(gross_weighted),
                mae_pips=mae_pips,
                mfe_pips=mfe_pips,
                mae_dollars=self._pips_to_dollars(
                    pips_weighted(mae_pips, qty=closed.qty, qty_ref=self.params.qty_ref)
                ),
                mfe_dollars=self._pips_to_dollars(
                    pips_weighted(mfe_pips, qty=closed.qty, qty_ref=self.params.qty_ref)
                ),
                bucket=closed.bucket,
                reason=closed.reason,
                gross_pnl_pips=gross_weighted,
                cost_pips=cost_weighted,
                net_pnl_pips=gross_weighted - cost_weighted,
                qty=closed.qty,
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
                qty=0.0,
            )
        pnl_pips = self._pnl_pips(is_long, self._leg_entry(pair, is_long), mark)
        as_of = self.last_bar.ts if self.last_bar is not None else pair.entry_ts
        costs = self._leg_cost(pair, is_long=is_long, as_of=as_of, exited=False)
        leg_qty = self._leg_qty(pair, is_long)
        gross_weighted = self._leg_weighted_pips(pair, is_long, pnl_pips)
        cost_weighted = costs.total_pips * (leg_qty / self.params.qty_ref)
        mae_pips = pair.long_mae_pips if is_long else pair.short_mae_pips
        mfe_pips = pair.long_mfe_pips if is_long else pair.short_mfe_pips
        return TradePairLeg(
            side=side,
            role=role,
            status="open",
            pnl_pips=pnl_pips,
            pnl_dollars=self._pips_to_dollars(gross_weighted),
            mae_pips=mae_pips,
            mfe_pips=mfe_pips,
            mae_dollars=self._pips_to_dollars(
                pips_weighted(mae_pips, qty=leg_qty, qty_ref=self.params.qty_ref)
            ),
            mfe_dollars=self._pips_to_dollars(
                pips_weighted(mfe_pips, qty=leg_qty, qty_ref=self.params.qty_ref)
            ),
            gross_pnl_pips=gross_weighted,
            cost_pips=cost_weighted,
            net_pnl_pips=gross_weighted - cost_weighted,
            qty=leg_qty,
        )
