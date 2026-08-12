"""Orchestrates one poll cycle: fetch 1M candles -> aggregate -> evaluate the IPDA
Supertrend entry on the forming bar -> gate (fire once per candle) -> submit to
mt5-trader when a configured trading session is open, otherwise notify only.

The same cycle samples the current price for every trade already filled, so the
break-even advisory (``position_tracker``) fires within one poll interval of the
trade reaching its maximum-favourable-excursion trigger.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .candles import Aggregator
from .config import Settings
from .data_client import MarketDataClient, Tick
from .instruments import InstrumentConfig
from .logging_config import RuntimeLogs, log_event
from .models import build_signal_payload
from .mt5_client import Mt5TraderClient
from .notifier import Notifier
from .position_tracker import PositionTracker, TrackerUpdate, tracked_trade_from_fill
from .sessions import active_session
from .signal_gate import SignalGate
from .strategy import Decision, IpdaSignalStrategy, StrategyParams


@dataclass(slots=True)
class _PendingSignal:
    instrument: InstrumentConfig
    decision: Decision
    bucket_start: Any
    payload: dict[str, Any]


class _InstrumentPipeline:
    def __init__(self, instrument: InstrumentConfig, settings: Settings) -> None:
        self.instrument = instrument
        self._gate = SignalGate()
        self._strategy = IpdaSignalStrategy(
            StrategyParams(
                sensitivity=settings.supertrend_sensitivity,
                atr_len=settings.supertrend_atr_len,
                sma_len=settings.sma_len,
                risk_reward=settings.risk_reward,
                send_stop_loss=settings.send_stop_loss,
                send_take_profit=settings.send_take_profit,
                use_hard_targets=settings.use_hard_targets,
                stop_loss_pips=instrument.resolved_stop_loss_pips(settings),
                take_profit_pips=instrument.resolved_take_profit_pips(settings),
                pip_size=instrument.resolved_pip_size(settings),
            )
        )

    def evaluate(self, series: Any, settings: Settings) -> _PendingSignal | None:
        if series.forming is None:
            return None

        bucket_start = series.forming.start
        symbol = self.instrument.resolved_mt5_symbol()
        if self._gate.is_locked(symbol, bucket_start):
            return None

        decision = self._strategy.evaluate(series)
        if decision is None:
            return None

        # Lock regardless of whether the signal will execute, so an out-of-session
        # candle produces exactly one notification instead of one per poll.
        self._gate.lock(symbol, bucket_start)
        payload = build_signal_payload(decision, self.instrument, settings)
        return _PendingSignal(
            instrument=self.instrument,
            decision=decision,
            bucket_start=bucket_start,
            payload=payload,
        )


class SignalService:
    def __init__(
        self,
        settings: Settings,
        data_client: MarketDataClient,
        mt5_client: Mt5TraderClient,
        logs: RuntimeLogs,
        notifier: Notifier | None = None,
        tracker: PositionTracker | None = None,
    ) -> None:
        self._s = settings
        self._data = data_client
        self._mt5 = mt5_client
        self._logs = logs
        self._notifier = notifier
        self._tracker = tracker
        self._sessions = settings.session_windows()
        self._aggregator = Aggregator(settings.target_tf_minutes, settings.bucket_offset_minutes)
        self._pipelines = [
            _InstrumentPipeline(instrument, settings) for instrument in settings.instruments
        ]

    async def tick(self) -> None:
        await self._track_open_trades()

        fetch_results = await asyncio.gather(
            *[self._fetch_for_pipeline(pipeline) for pipeline in self._pipelines],
            return_exceptions=True,
        )

        pending: list[_PendingSignal] = []
        for pipeline, result in zip(self._pipelines, fetch_results, strict=True):
            if isinstance(result, BaseException):
                self._logs.errors.append(
                    {
                        "kind": "data_poll_failed",
                        "quote": pipeline.instrument.quote,
                        "error": str(result),
                    }
                )
                continue

            signal = pipeline.evaluate(result, self._s)
            if signal is not None:
                pending.append(signal)

        if not pending:
            return

        session = active_session(datetime.now(UTC), self._sessions)
        if session is None:
            await self._skip_out_of_session(pending)
            return

        for signal in pending:
            self._logs.signals.append(
                {
                    "kind": "signal_fired",
                    "quote": signal.instrument.quote,
                    "symbol": signal.instrument.resolved_mt5_symbol(),
                    "direction": signal.decision.direction,
                    "bucket_start": signal.bucket_start.isoformat(),
                    "session": session,
                    "entry": signal.decision.entry,
                    "stop_loss": signal.decision.stop_loss,
                    "take_profit": signal.decision.take_profit,
                    "supertrend": signal.decision.supertrend,
                    "signal_id": signal.payload["signal_id"],
                }
            )
            log_event(
                "signal_fired",
                quote=signal.instrument.quote,
                symbol=signal.instrument.resolved_mt5_symbol(),
                direction=signal.decision.direction,
                bucket_start=signal.bucket_start.isoformat(),
                session=session,
                signal_id=signal.payload["signal_id"],
                entry=signal.decision.entry,
            )

        submit_results = await asyncio.gather(
            *[self._mt5.submit(signal.payload) for signal in pending],
            return_exceptions=True,
        )

        for signal, outcome in zip(pending, submit_results, strict=True):
            if isinstance(outcome, BaseException):
                self._logs.executions.append(
                    {
                        "signal_id": signal.payload["signal_id"],
                        "quote": signal.instrument.quote,
                        "outcome": "error",
                        "detail": {"error": str(outcome)},
                    }
                )
                log_event(
                    "signal_submitted",
                    level=logging.ERROR,
                    quote=signal.instrument.quote,
                    signal_id=signal.payload["signal_id"],
                    outcome="error",
                    error=str(outcome),
                )
                continue

            self._logs.executions.append(
                {
                    "signal_id": signal.payload["signal_id"],
                    "quote": signal.instrument.quote,
                    "outcome": outcome.kind,
                    "status_code": outcome.status_code,
                    "detail": outcome.detail,
                }
            )
            level = logging.INFO if outcome.kind == "success" else logging.WARNING
            log_event(
                "signal_submitted",
                level=level,
                quote=signal.instrument.quote,
                signal_id=signal.payload["signal_id"],
                outcome=outcome.kind,
                status_code=outcome.status_code,
            )
            if outcome.kind == "unauthorized":
                log_event(
                    "mt5_unauthorized",
                    level=logging.ERROR,
                    quote=signal.instrument.quote,
                    signal_id=signal.payload["signal_id"],
                )
            if outcome.kind == "success":
                self._start_tracking(signal, outcome.detail)

    # -- session gate ------------------------------------------------------

    async def _skip_out_of_session(self, pending: list[_PendingSignal]) -> None:
        session_names = ", ".join(window.name for window in self._sessions)
        for signal in pending:
            symbol = signal.instrument.resolved_mt5_symbol()
            record = {
                "kind": "signal_skipped_out_of_session",
                "quote": signal.instrument.quote,
                "symbol": symbol,
                "direction": signal.decision.direction,
                "bucket_start": signal.bucket_start.isoformat(),
                "entry": signal.decision.entry,
                "sessions": session_names,
                "signal_id": signal.payload["signal_id"],
            }
            self._logs.signals.append(record)
            log_event(
                "signal_skipped_out_of_session",
                level=logging.WARNING,
                quote=signal.instrument.quote,
                symbol=symbol,
                direction=signal.decision.direction,
                bucket_start=signal.bucket_start.isoformat(),
                signal_id=signal.payload["signal_id"],
            )
            await self._notify_skipped(signal, session_names)

    async def _notify_skipped(self, signal: _PendingSignal, session_names: str) -> None:
        if self._notifier is None:
            return
        symbol = signal.instrument.resolved_mt5_symbol()
        direction = signal.decision.direction.upper()
        pip_size = signal.instrument.resolved_pip_size(self._s)
        await self._notifier.send(
            subject=f"{symbol} {direction} — not executed (outside session)",
            lines=[
                f"symbol: {symbol}",
                f"direction: {signal.decision.direction}",
                f"entry (bar close): {signal.decision.entry}",
                f"would-be stop: {signal.instrument.resolved_stop_loss_pips(self._s)} pips",
                f"would-be target: {signal.instrument.resolved_take_profit_pips(self._s)} pips",
                f"pip size: {pip_size}",
                f"bucket_start: {signal.bucket_start.isoformat()}",
                f"trading sessions: {session_names}",
                "reason: signal fired outside the configured trading sessions; "
                "no order was submitted.",
            ],
            signal_id=signal.payload["signal_id"],
        )

    # -- break-even advisory ----------------------------------------------

    def _start_tracking(self, signal: _PendingSignal, detail: dict[str, Any] | None) -> None:
        if self._tracker is None:
            return
        trade = tracked_trade_from_fill(
            signal_id=str(signal.payload["signal_id"]),
            quote=signal.instrument.quote,
            symbol=signal.instrument.resolved_mt5_symbol(),
            direction=signal.decision.direction,
            fallback_entry=signal.decision.entry,
            pip_size=signal.instrument.resolved_pip_size(self._s),
            stop_loss_pips=signal.instrument.resolved_stop_loss_pips(self._s),
            take_profit_pips=signal.instrument.resolved_take_profit_pips(self._s),
            detail=detail,
        )
        if self._tracker.track(trade):
            log_event(
                "trade_tracking_started",
                symbol=trade.symbol,
                direction=trade.direction,
                entry=trade.entry,
                signal_id=trade.signal_id,
            )

    async def _track_open_trades(self) -> None:
        if self._tracker is None:
            return

        for update in self._tracker.expire():
            self._log_tracker_update(update)

        quotes = self._tracker.quotes()
        if not quotes:
            return

        ticks = await asyncio.gather(
            *[self._data.fetch_tick(quote) for quote in quotes],
            return_exceptions=True,
        )
        for quote, tick in zip(quotes, ticks, strict=True):
            if isinstance(tick, BaseException):
                self._logs.errors.append(
                    {"kind": "tick_poll_failed", "quote": quote, "error": str(tick)}
                )
                continue
            for update in self._tracker.observe(quote, tick):
                self._log_tracker_update(update)
                if update.break_even_reached:
                    await self._notify_break_even(update, tick)

    def _log_tracker_update(self, update: TrackerUpdate) -> None:
        trade = update.trade
        if update.break_even_reached:
            record = {
                "kind": "break_even_reached",
                "symbol": trade.symbol,
                "direction": trade.direction,
                "entry": trade.entry,
                "mfe_pips": round(trade.mfe_pips, 2),
                "trigger_pips": self._s.mfe_break_even_pips,
                "signal_id": trade.signal_id,
            }
            self._logs.signals.append(record)
            log_event("break_even_reached", **record)
        if update.closed_reason is not None:
            log_event(
                "tracked_trade_closed",
                symbol=trade.symbol,
                direction=trade.direction,
                reason=update.closed_reason,
                mfe_pips=round(trade.mfe_pips, 2),
                mae_pips=round(trade.mae_pips, 2),
                signal_id=trade.signal_id,
            )

    async def _notify_break_even(self, update: TrackerUpdate, tick: Tick) -> None:
        if self._notifier is None:
            return
        trade = update.trade
        price = tick.bid if trade.direction == "buy" else tick.ask
        await self._notifier.send(
            subject=f"{trade.symbol} {trade.direction.upper()} — move stop to break-even",
            lines=[
                f"symbol: {trade.symbol}",
                f"direction: {trade.direction}",
                f"entry: {trade.entry}",
                f"current: {price}",
                f"peak favourable excursion: {trade.mfe_pips:.1f} pips",
                f"trigger: {self._s.mfe_break_even_pips} pips",
                f"signal_id: {trade.signal_id}",
                "action: move the stop-loss to the entry price in MT5. "
                "This service does not modify stops.",
            ],
            signal_id=trade.signal_id,
        )

    async def _fetch_for_pipeline(self, pipeline: _InstrumentPipeline) -> Any:
        minutes = await self._data.fetch_minute_candles(pipeline.instrument.quote)
        return self._aggregator.build(minutes)
