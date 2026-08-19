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
    long_open: bool = True
    short_open: bool = True
    locked: bool = False
    entry_ts: datetime = field(default_factory=datetime.now)


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


class ClosedBarEngine:
    """One step per closed 15m bar. Fill is the next bar's open."""

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
                unrealized += self._pnl(True, pair.entry, last_close)
            if pair.short_open:
                unrealized += self._pnl(False, pair.entry, last_close)
        open_pairs = sum(1 for pair in self.pairs if pair.long_open or pair.short_open)
        return BacktestReport(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            bar_count=0,
            realized=self.stats.realized,
            unrealized=unrealized,
            equity=self.params.initial_capital + self.stats.realized + unrealized,
            long_wins=self.stats.long_wins,
            long_be=self.stats.long_be,
            long_loss=self.stats.long_loss,
            short_wins=self.stats.short_wins,
            short_be=self.stats.short_be,
            short_loss=self.stats.short_loss,
            locks=self.stats.locks,
            open_pairs=open_pairs,
            trades=list(self.trades),
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
                        long_open=bool(raw["long_open"]),
                        short_open=bool(raw["short_open"]),
                        locked=bool(raw["locked"]),
                        entry_ts=datetime.fromisoformat(str(raw["entry_ts"])),
                    )
                )
        stats_raw = payload.get("stats")
        if isinstance(stats_raw, dict):
            self.stats = Stats.model_validate(stats_raw)
        trades_raw = payload.get("trades")
        self.trades = []
        if isinstance(trades_raw, list):
            self.trades = [ClosedLeg.model_validate(item) for item in trades_raw]

    def _pnl(self, is_long: bool, entry: float, exit_px: float) -> float:
        delta = (exit_px - entry) if is_long else (entry - exit_px)
        return delta * self.params.qty * self.params.point_value

    def _bucket(self, is_long: bool, entry: float, exit_px: float) -> Literal["win", "be", "loss"]:
        pnl_px = (exit_px - entry) if is_long else (entry - exit_px)
        if pnl_px > self.be_eps:
            return "win"
        if pnl_px < -self.be_eps:
            return "loss"
        return "be"

    def _fill_pending(self, bar: Candle) -> None:
        if not self.pending:
            return
        for session, signal in list(self.pending.items()):
            self._open_pair(session, bar.open, signal.range_price, bar.ts, signal.bullish)
            del self.pending[session]

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
            entry_ts=ts,
        )
        self.pairs.append(pair)
        self.events.append(
            EngineEvent(
                kind="entry",
                session=session,
                ts=ts,
                detail={
                    "entry": entry,
                    "sl_dist": sl_dist,
                    "sl_pips": sl_dist / self.params.pip_size,
                    "bullish_signal": bullish,
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

    def _close_long(self, pair: Pair, px: float, ts: datetime) -> None:
        if not pair.long_open:
            return
        self._record_close(pair, is_long=True, px=px, ts=ts)
        pair.long_open = False

    def _close_short(self, pair: Pair, px: float, ts: datetime) -> None:
        if not pair.short_open:
            return
        self._record_close(pair, is_long=False, px=px, ts=ts)
        pair.short_open = False

    def _record_close(self, pair: Pair, *, is_long: bool, px: float, ts: datetime) -> None:
        side: Literal["long", "short"] = "long" if is_long else "short"
        bucket = self._bucket(is_long, pair.entry, px)
        pnl = self._pnl(is_long, pair.entry, px)
        self.stats.realized += pnl
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
                entry=pair.entry,
                exit=px,
                pnl=pnl,
                bucket=bucket,
                ts=ts,
                reason="sl_or_tp",
            )
        )
        self.events.append(
            EngineEvent(
                kind="exit",
                session=pair.session,
                ts=ts,
                detail={"side": side, "exit": px, "bucket": bucket, "pnl": pnl},
            )
        )
