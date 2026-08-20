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

    def _fill_pending(self, bar: Candle) -> None:
        if not self.pending:
            return
        for session, signal in list(self.pending.items()):
            del self.pending[session]
            self._open_pair(session, bar.open, signal.range_price, bar.ts, signal.bullish)

    def _open_pair(
        self, session: str, entry: float, range_price: float, ts: datetime, bullish: bool
    ) -> None:
        sl_dist = max(
            range_price * self.params.sl_mult, self.params.min_stop_pips * self.params.pip_size
        )
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
        is_doji = self.params.skip_doji and bar.close == bar.open
        valid_range = (bar.high - bar.low) > 0
        for window in self.windows:
            in_now = window.contains(open_ts)
            was = self.prev_in_session.get(window.name, False)
            if in_now and not was and valid_range and not is_doji:
                self.pending[window.name] = PendingSignal(
                    session=window.name,
                    range_price=bar.high - bar.low,
                    bullish=bar.close > bar.open,
                    signal_ts=bar.ts,
                )
                self.events.append(
                    EngineEvent(
                        kind="signal",
                        session=window.name,
                        ts=bar.ts,
                        detail={
                            "range": bar.high - bar.low,
                            "bullish": bar.close > bar.open,
                        },
                    )
                )
            self.prev_in_session[window.name] = in_now

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
