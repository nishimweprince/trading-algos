"""Closed-bar session-open hedge engine. Shared by backtest and paper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from models import (
    BacktestReport,
    Candle,
    ClosedLeg,
    EngineEvent,
    EngineParams,
    OpenPairView,
    Stats,
    StrategyMode,
    Timeframe,
    TradePairLeg,
    TradePairResult,
)
from sessions import SessionWindow


@dataclass
class PendingSignal:
    session: str
    range_price: float
    bullish: bool
    signal_ts: datetime
    bars_remaining: int = 0
    sweep_high: float = 0.0
    sweep_low: float = 0.0
    first_open: float = 0.0
    last_close: float = 0.0


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
    primary_side: Literal["long", "short"] | None = None
    long_open: bool = True
    short_open: bool = True
    locked: bool = False
    entry_ts: datetime = field(default_factory=datetime.now)
    long_entry: float | None = None
    short_entry: float | None = None
    hedge_pending_side: Literal["long", "short"] | None = None
    hedge_pending_stop: float | None = None
    sweep_high: float | None = None
    sweep_low: float | None = None


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
    """One step per closed bar of the configured timeframe. Fill is the next bar's open."""

    def __init__(self, windows: list[SessionWindow], params: EngineParams) -> None:
        self.windows = windows
        self._windows_by_name = {window.name: window for window in windows}
        self.params = params
        self.pairs: list[Pair] = []
        self.pending: dict[str, PendingSignal] = {}
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

    def observe(self, bar: Candle) -> None:
        """Record session membership without trading. Used to warm paper on first tick."""
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for window in self.windows:
            self.prev_in_session[window.name] = window.contains(open_ts)
        self.last_bar = bar

    def step(self, bar: Candle) -> list[EngineEvent]:
        started = len(self.events)
        self._fill_pending(bar)
        self._manage_pairs(bar)
        self._arm_signals(bar)
        self.last_bar = bar
        self._record_equity(bar.close)
        return self.events[started:]

    def run(self, candles: list[Candle]) -> None:
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
        realized_dollars = self._pips_to_dollars(self.stats.realized_pips)
        unrealized_dollars = self._pips_to_dollars(unrealized_pips)
        max_drawdown_dollars = self._pips_to_dollars(self.max_drawdown_pips)
        open_pairs = sum(1 for pair in self.pairs if pair.long_open or pair.short_open)
        return BacktestReport(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            bar_count=0,
            performance_unit=self.params.performance_unit,
            realized=self.stats.realized,
            unrealized=unrealized,
            equity=self.params.initial_capital + self.stats.realized + unrealized,
            realized_pips=self.stats.realized_pips,
            unrealized_pips=unrealized_pips,
            max_drawdown_pips=self.max_drawdown_pips,
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
            "pending": {
                name: {
                    "session": signal.session,
                    "range_price": signal.range_price,
                    "bullish": signal.bullish,
                    "signal_ts": signal.signal_ts.isoformat(),
                    "bars_remaining": signal.bars_remaining,
                    "sweep_high": signal.sweep_high,
                    "sweep_low": signal.sweep_low,
                    "first_open": signal.first_open,
                    "last_close": signal.last_close,
                }
                for name, signal in self.pending.items()
            },
            "pairs": [
                asdict(pair) | {"entry_ts": pair.entry_ts.isoformat()} for pair in self.pairs
            ],
            "stats": self.stats.model_dump(),
            "trades": [leg.model_dump(mode="json") for leg in self.trades],
        }

    def restore(self, payload: dict[str, object]) -> None:
        prev = payload.get("prev_in_session")
        if isinstance(prev, dict):
            self.prev_in_session = {str(k): bool(v) for k, v in prev.items()}
        pending_raw = payload.get("pending")
        self.pending = {}
        if isinstance(pending_raw, dict):
            for name, raw in pending_raw.items():
                if not isinstance(raw, dict):
                    continue
                self.pending[str(name)] = PendingSignal(
                    session=str(raw["session"]),
                    range_price=float(raw["range_price"]),
                    bullish=bool(raw["bullish"]),
                    signal_ts=datetime.fromisoformat(str(raw["signal_ts"])),
                    bars_remaining=int(raw.get("bars_remaining", 0)),
                    sweep_high=float(raw.get("sweep_high", 0.0)),
                    sweep_low=float(raw.get("sweep_low", 0.0)),
                    first_open=float(raw.get("first_open", 0.0)),
                    last_close=float(raw.get("last_close", 0.0)),
                )
        pairs_raw = payload.get("pairs")
        self.pairs = []
        if isinstance(pairs_raw, list):
            for raw in pairs_raw:
                if not isinstance(raw, dict):
                    continue
                hedge_side = raw.get("hedge_pending_side")
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
                        hedge_pending_side=(
                            str(hedge_side) if hedge_side in {"long", "short"} else None
                        ),
                        hedge_pending_stop=_optional_float(raw.get("hedge_pending_stop")),
                        sweep_high=_optional_float(raw.get("sweep_high")),
                        sweep_low=_optional_float(raw.get("sweep_low")),
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

    def _pnl(self, is_long: bool, entry: float, exit_px: float) -> float:
        delta = (exit_px - entry) if is_long else (entry - exit_px)
        return delta * self.params.qty * self.params.point_value

    def _pnl_pips(self, is_long: bool, entry: float, exit_px: float) -> float:
        delta = (exit_px - entry) if is_long else (entry - exit_px)
        return delta / self.params.pip_size

    def _pips_to_dollars(self, pips: float) -> float | None:
        rate = self.params.dollars_per_pip_per_qty
        if rate is None:
            return None
        return pips * rate * self.params.qty

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

    def _record_equity(self, mark: float) -> None:
        equity_pips = self.stats.realized_pips + self._unrealized_pips(mark)
        self.equity_peak_pips = max(self.equity_peak_pips, equity_pips)
        self.max_drawdown_pips = max(
            self.max_drawdown_pips, self.equity_peak_pips - equity_pips
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

    def _active_pair_count(self) -> int:
        return sum(
            1
            for pair in self.pairs
            if pair.long_open or pair.short_open or pair.hedge_pending_side is not None
        )

    def _at_open_pair_cap(self) -> bool:
        cap = self.params.max_open_pairs
        return cap > 0 and self._active_pair_count() >= cap

    def _stop_too_wide(self, sl_dist: float) -> bool:
        cap = self.params.max_stop_pips
        return cap > 0 and sl_dist / self.params.pip_size > cap

    def _fill_pending(self, bar: Candle) -> None:
        if not self.pending:
            return
        sweep = self.params.strategy_mode == StrategyMode.SWEEP_FADE
        for session, signal in list(self.pending.items()):
            if sweep and signal.bars_remaining > 1:
                signal.sweep_high = max(signal.sweep_high, bar.high)
                signal.sweep_low = min(signal.sweep_low, bar.low)
                signal.last_close = bar.close
                signal.range_price = signal.sweep_high - signal.sweep_low
                signal.bullish = signal.last_close > signal.first_open
                signal.bars_remaining -= 1
                continue
            del self.pending[session]
            if sweep:
                self._open_sweep_fade(session, bar.open, signal, bar.ts)
            else:
                self._open_pair(session, bar.open, signal.range_price, bar.ts, signal.bullish)

    def _open_pair(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> None:
        sl_dist = max(
            range_price * self.params.sl_mult, self.params.min_stop_pips * self.params.pip_size
        )
        if sl_dist <= 0 or self._stop_too_wide(sl_dist) or self._at_open_pair_cap():
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
            primary_side="long" if bullish else "short",
            entry_ts=ts,
            long_entry=entry,
            short_entry=entry,
        )
        self.pairs.append(pair)
        self._emit_entry(pair, ts, bullish_signal=bullish)

    def _open_sweep_fade(
        self, session: str, entry: float, signal: PendingSignal, ts: datetime
    ) -> None:
        sweep_high = signal.sweep_high
        sweep_low = signal.sweep_low
        range_price = sweep_high - sweep_low
        if range_price <= 0:
            return
        if self.params.skip_doji and signal.last_close == signal.first_open:
            return
        if self._stop_too_wide(range_price) or self._at_open_pair_cap():
            return
        fade_long = signal.last_close < signal.first_open
        if fade_long:
            sl_dist = entry - sweep_low
            if sl_dist <= 0 or self._stop_too_wide(sl_dist):
                return
            pair = Pair(
                id=f"{session}:{ts.isoformat()}",
                session=session,
                entry=entry,
                sl_dist=sl_dist,
                long_sl=sweep_low,
                long_tp=entry + sl_dist * self.params.rr,
                short_sl=sweep_low,
                short_tp=entry - sl_dist * self.params.rr,
                primary_side="long",
                long_open=True,
                short_open=False,
                entry_ts=ts,
                long_entry=entry,
                hedge_pending_side="short",
                hedge_pending_stop=sweep_low,
                sweep_high=sweep_high,
                sweep_low=sweep_low,
            )
        else:
            sl_dist = sweep_high - entry
            if sl_dist <= 0 or self._stop_too_wide(sl_dist):
                return
            pair = Pair(
                id=f"{session}:{ts.isoformat()}",
                session=session,
                entry=entry,
                sl_dist=sl_dist,
                long_sl=sweep_high,
                long_tp=entry + sl_dist * self.params.rr,
                short_sl=sweep_high,
                short_tp=entry - sl_dist * self.params.rr,
                primary_side="short",
                long_open=False,
                short_open=True,
                entry_ts=ts,
                short_entry=entry,
                hedge_pending_side="long",
                hedge_pending_stop=sweep_high,
                sweep_high=sweep_high,
                sweep_low=sweep_low,
            )
        self.pairs.append(pair)
        self._emit_entry(pair, ts, bullish_signal=signal.last_close > signal.first_open)

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
                    "strategy_mode": self.params.strategy_mode,
                    "hedge_pending_side": pair.hedge_pending_side,
                },
            )
        )

    def _arm_signals(self, bar: Candle) -> None:
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        is_doji = self.params.skip_doji and bar.close == bar.open
        valid_range = (bar.high - bar.low) > 0
        sweep = self.params.strategy_mode == StrategyMode.SWEEP_FADE
        for window in self.windows:
            in_now = window.contains(open_ts)
            was = self.prev_in_session.get(window.name, False)
            if in_now and not was and valid_range and (sweep or not is_doji):
                delay = self.params.signal_delay_bars
                if sweep and delay <= 0:
                    delay = 2
                self.pending[window.name] = PendingSignal(
                    session=window.name,
                    range_price=bar.high - bar.low,
                    bullish=bar.close > bar.open,
                    signal_ts=bar.ts,
                    bars_remaining=delay if sweep else 0,
                    sweep_high=bar.high,
                    sweep_low=bar.low,
                    first_open=bar.open,
                    last_close=bar.close,
                )
                self.events.append(
                    EngineEvent(
                        kind="signal",
                        session=window.name,
                        ts=bar.ts,
                        detail={
                            "range": bar.high - bar.low,
                            "bullish": bar.close > bar.open,
                            "delay_bars": delay if sweep else 0,
                        },
                    )
                )
            self.prev_in_session[window.name] = in_now

    def _manage_pairs(self, bar: Candle) -> None:
        self._flatten_ended_sessions(bar)
        if self.params.strategy_mode == StrategyMode.SWEEP_FADE:
            for pair in self.pairs:
                if pair.long_open or pair.short_open or pair.hedge_pending_side is not None:
                    self._manage_sweep_fade(pair, bar)
            return
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
                if long_hit_sl and pair.short_open:
                    self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
                    self._apply_lock(pair, long_survives=False, ts=bar.ts)
                    if bar.low <= pair.short_tp:
                        self._close_short(
                            pair, _fill_limit(bar.open, pair.short_tp, False), bar.ts
                        )
                elif short_hit_sl and pair.long_open:
                    self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
                    self._apply_lock(pair, long_survives=True, ts=bar.ts)
                    if bar.high >= pair.long_tp:
                        self._close_long(pair, _fill_limit(bar.open, pair.long_tp, True), bar.ts)
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
            if pair.locked and (pair.long_open or pair.short_open):
                self._trail_survivor(pair, bar)

    def _flatten_ended_sessions(self, bar: Candle) -> None:
        if not self.params.flatten_at_session_end:
            return
        open_ts = bar_open(bar, self.params.timeframe_minutes)
        for pair in self.pairs:
            if not pair.long_open and not pair.short_open:
                pair.hedge_pending_side = None
                pair.hedge_pending_stop = None
                continue
            window = self._windows_by_name.get(pair.session)
            if window is None or window.contains(open_ts):
                continue
            if pair.long_open:
                self._close_long(pair, bar.open, bar.ts, reason="session_end")
            if pair.short_open:
                self._close_short(pair, bar.open, bar.ts, reason="session_end")
            pair.hedge_pending_side = None
            pair.hedge_pending_stop = None

    def _manage_sweep_fade(self, pair: Pair, bar: Candle) -> None:
        self._fill_invalidation_hedge(pair, bar)
        if pair.long_open:
            if bar.low <= pair.long_sl:
                self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
            elif bar.high >= pair.long_tp:
                self._close_long(pair, _fill_limit(bar.open, pair.long_tp, True), bar.ts)
        if pair.short_open:
            if bar.high >= pair.short_sl:
                self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
            elif bar.low <= pair.short_tp:
                self._close_short(pair, _fill_limit(bar.open, pair.short_tp, False), bar.ts)
        if not pair.locked:
            if pair.long_open and not pair.short_open and self.lock_dist > 0:
                if bar.high >= self._leg_entry(pair, True) + self.lock_dist:
                    self._apply_profit_lock(pair, long_working=True, ts=bar.ts)
            elif pair.short_open and not pair.long_open and self.lock_dist > 0:
                if bar.low <= self._leg_entry(pair, False) - self.lock_dist:
                    self._apply_profit_lock(pair, long_working=False, ts=bar.ts)
        if pair.locked and (pair.long_open or pair.short_open):
            self._trail_survivor(pair, bar)
        if not pair.long_open and not pair.short_open:
            pair.hedge_pending_side = None
            pair.hedge_pending_stop = None

    def _fill_invalidation_hedge(self, pair: Pair, bar: Candle) -> None:
        side = pair.hedge_pending_side
        stop = pair.hedge_pending_stop
        if side is None or stop is None:
            return
        if side == "long" and bar.high >= stop:
            fill = _fill_stop(bar.open, stop, going_down=False)
            if pair.short_open:
                self._close_short(pair, _fill_stop(bar.open, pair.short_sl, False), bar.ts)
            self._activate_hedge(pair, is_long=True, px=fill, ts=bar.ts)
        elif side == "short" and bar.low <= stop:
            fill = _fill_stop(bar.open, stop, going_down=True)
            if pair.long_open:
                self._close_long(pair, _fill_stop(bar.open, pair.long_sl, True), bar.ts)
            self._activate_hedge(pair, is_long=False, px=fill, ts=bar.ts)

    def _activate_hedge(self, pair: Pair, *, is_long: bool, px: float, ts: datetime) -> None:
        pair.hedge_pending_side = None
        pair.hedge_pending_stop = None
        pair.locked = False
        opposite = pair.sweep_low if is_long else pair.sweep_high
        if opposite is None:
            sl_dist = pair.sl_dist
            sl = px - sl_dist if is_long else px + sl_dist
        else:
            sl = opposite
            sl_dist = (px - sl) if is_long else (sl - px)
        if sl_dist <= 0:
            sl_dist = max(pair.sl_dist, self.params.min_stop_pips * self.params.pip_size)
            sl = px - sl_dist if is_long else px + sl_dist
        pair.sl_dist = sl_dist
        if is_long:
            pair.long_entry = px
            pair.long_open = True
            pair.long_sl = sl
            pair.long_tp = px + sl_dist * self.params.rr
        else:
            pair.short_entry = px
            pair.short_open = True
            pair.short_sl = sl
            pair.short_tp = px - sl_dist * self.params.rr
        self.events.append(
            EngineEvent(
                kind="entry",
                session=pair.session,
                ts=ts,
                detail={
                    "entry": px,
                    "sl_dist": sl_dist,
                    "sl_pips": sl_dist / self.params.pip_size,
                    "primary_side": pair.primary_side,
                    "pair_id": pair.id,
                    "role": "hedge",
                    "hedge_side": "long" if is_long else "short",
                },
            )
        )

    def _apply_profit_lock(self, pair: Pair, *, long_working: bool, ts: datetime) -> None:
        entry = self._leg_entry(pair, long_working)
        if long_working:
            lock_sl = entry + self.lock_dist if self.lock_dist > 0 else entry
            pair.long_sl = max(pair.long_sl, lock_sl)
            new_sl = pair.long_sl
        else:
            lock_sl = entry - self.lock_dist if self.lock_dist > 0 else entry
            pair.short_sl = min(pair.short_sl, lock_sl)
            new_sl = pair.short_sl
        pair.locked = True
        self.stats.locks += 1
        self.events.append(
            EngineEvent(
                kind="lock",
                session=pair.session,
                ts=ts,
                detail={"long_survives": long_working, "new_sl": new_sl, "profit_lock": True},
            )
        )

    def _trail_survivor(self, pair: Pair, bar: Candle) -> None:
        step = self.params.trail_step_pips * self.params.pip_size
        if step <= 0 or not pair.locked:
            return
        if pair.long_open and not pair.short_open:
            while bar.high - pair.long_sl >= step:
                pair.long_sl += step
        elif pair.short_open and not pair.long_open:
            while pair.short_sl - bar.low >= step:
                pair.short_sl -= step

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
        role: Literal["primary", "hedge", "unknown"]
        if pair.primary_side is None:
            role = "unknown"
        else:
            role = "primary" if side == pair.primary_side else "hedge"
        self.stats.realized += pnl
        self.stats.realized_pips += pnl_pips
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
            return TradePairLeg(
                side=side,
                role=role,
                status="closed",
                exit=closed.exit,
                exit_ts=closed.ts,
                pnl_pips=pnl_pips,
                pnl_dollars=self._pips_to_dollars(pnl_pips),
                bucket=closed.bucket,
                reason=closed.reason,
            )
        opened = pair.long_open if is_long else pair.short_open
        if not opened:
            return TradePairLeg(
                side=side,
                role=role,
                status="closed",
                pnl_pips=0.0,
                pnl_dollars=self._pips_to_dollars(0.0),
                reason="not_filled",
            )
        pnl_pips = self._pnl_pips(is_long, self._leg_entry(pair, is_long), mark)
        return TradePairLeg(
            side=side,
            role=role,
            status="open",
            pnl_pips=pnl_pips,
            pnl_dollars=self._pips_to_dollars(pnl_pips),
        )
