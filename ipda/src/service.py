"""Orchestrates one poll cycle: fetch 1M candles -> evaluate the configured entry -> gate
(fire once per candle) -> submit to mt5-trader when a configured trading session is open,
otherwise notify only.

``STRATEGY=reversal`` (default) aggregates the minutes and evaluates the RSI Buy/Sell
Chance on the forming bar. ``STRATEGY=ipda`` streams new minutes through the stateful
IPDA model (docs/ipda-model.md), seeded once from D1 candles, and sends limit orders.

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

from ta_notify import Notifier

from .candles import Aggregator
from .config import Settings
from .data_client import MarketDataClient, Tick
from .instruments import InstrumentConfig
from .ipda_model import DEFAULT_KILLZONES, IpdaModel, IpdaParams, Setup, trading_day
from .logging_config import RuntimeLogs, log_event
from .models import build_signal_payload
from .mt5_client import Mt5TraderClient
from .position_tracker import PositionTracker, TrackerUpdate, tracked_trade_from_fill
from .sessions import active_session
from .signal_gate import SignalGate
from .strategy import IPDA, Decision, ReversalParams, ReversalSignalStrategy


@dataclass(slots=True)
class _PendingSignal:
    instrument: InstrumentConfig
    decision: Decision
    bucket_start: Any
    payload: dict[str, Any]


@dataclass(slots=True)
class _IpdaInput:
    minutes: list[Any]
    daily: list[Any] | None


class _InstrumentPipeline:
    def __init__(self, instrument: InstrumentConfig, settings: Settings) -> None:
        self.instrument = instrument
        self._gate = SignalGate()
        self._strategy = ReversalSignalStrategy(
            ReversalParams(
                rsi_len=settings.reversal_rsi_len,
                oversold=settings.reversal_oversold,
                overbought=settings.reversal_overbought,
                send_stop_loss=settings.send_stop_loss,
                send_take_profit=settings.send_take_profit,
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
            _IpdaPipeline(instrument, settings)
            if settings.strategy == "ipda"
            else _InstrumentPipeline(instrument, settings)
            for instrument in settings.instruments
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
                    "trigger": signal.decision.trigger,
                    "trigger_value": signal.decision.trigger_value,
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
            if outcome.kind == "success" and signal.decision.execution_type == "market":
                # A resting limit has no fill yet, so there is nothing to track.
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
                *_target_lines(signal, self._s),
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

    async def _fetch_for_pipeline(self, pipeline: _InstrumentPipeline | _IpdaPipeline) -> Any:
        quote = pipeline.instrument.quote
        minutes = await self._data.fetch_minute_candles(quote)
        if isinstance(pipeline, _IpdaPipeline):
            daily = None
            if not pipeline.seeded:
                daily = await self._data.fetch_daily_candles(quote, self._s.ipda_range_days + 5)
            return _IpdaInput(minutes=minutes, daily=daily)
        return self._aggregator.build(minutes)


def _target_lines(signal: _PendingSignal, settings: Settings) -> list[str]:
    d = signal.decision
    if d.stop_loss is not None and d.take_profit is not None:
        return [
            f"order: {d.execution_type} @ {d.entry}",
            f"would-be stop: {d.stop_loss}",
            f"would-be target: {d.take_profit}",
        ]
    return [
        f"would-be stop: {signal.instrument.resolved_stop_loss_pips(settings)} pips",
        f"would-be target: {signal.instrument.resolved_take_profit_pips(settings)} pips",
    ]


class _IpdaPipeline:
    """Feeds each newly closed minute to a long-lived IpdaModel.

    The first poll is a warm-up: it seeds the data range from D1 candles and replays the
    lookback window so the Asian range and structure are known, but any setup it finds is
    history and is dropped. Afterwards only minutes newer than the last one seen are fed,
    so every setup is produced by the poll that observed its confirming bar.
    """

    def __init__(self, instrument: InstrumentConfig, settings: Settings) -> None:
        self.instrument = instrument
        self._gate = SignalGate()
        pip = instrument.resolved_pip_size(settings)
        zones = tuple(z for z in DEFAULT_KILLZONES if z.name in settings.ipda_killzones)
        self.model = IpdaModel(
            IpdaParams(
                tf_minutes=settings.target_tf_minutes,
                range_days=settings.ipda_range_days,
                pd_filter=settings.ipda_pd_filter,
                killzones=zones,
                risk_reward=settings.ipda_risk_reward,
                target_mode=settings.ipda_target,
                stop_buffer=settings.ipda_stop_buffer_pips * pip,
                min_risk=settings.ipda_min_risk_pips * pip,
                max_risk=settings.ipda_max_risk_pips * pip,
            )
        )
        self.seeded = False

    def evaluate(self, data: _IpdaInput, settings: Settings) -> _PendingSignal | None:
        now = datetime.now(UTC)
        warm_up = not self.seeded
        if warm_up:
            if not data.daily:
                log_event("ipda_seed_missing", level=logging.WARNING, quote=self.instrument.quote)
                return None
            today = trading_day(now)
            days = [(trading_day(c.start), c.high, c.low) for c in data.daily]
            self.model.seed_daily([d for d in days if d[0] < today])
            self.seeded = True
            log_event(
                "ipda_seeded",
                quote=self.instrument.quote,
                days=len(days),
                equilibrium=self.model.equilibrium(),
            )

        latest: Setup | None = None
        for minute in data.minutes:
            setup = self.model.on_minute(minute)
            if setup is not None:
                latest = setup
        if warm_up or latest is None or latest.expires_at <= now:
            return None

        symbol = self.instrument.resolved_mt5_symbol()
        if self._gate.is_locked(symbol, latest.bucket_start):
            return None
        self._gate.lock(symbol, latest.bucket_start)
        decision = Decision(
            direction=latest.direction,
            bucket_start=latest.bucket_start,
            entry=latest.entry,
            stop_loss=latest.stop_loss,
            take_profit=latest.take_profit,
            trigger=IPDA,
            trigger_value=latest.equilibrium if latest.equilibrium is not None else 0.0,
            execution_type="limit",
            expires_at=latest.expires_at,
        )
        log_event(
            "ipda_setup",
            quote=self.instrument.quote,
            direction=latest.direction,
            killzone=latest.killzone,
            entry=latest.entry,
            stop_loss=latest.stop_loss,
            take_profit=latest.take_profit,
            sweep_extreme=latest.sweep_extreme,
            fvg=[latest.fvg_bottom, latest.fvg_top],
            equilibrium=latest.equilibrium,
            expires_at=latest.expires_at.isoformat(),
        )
        return _PendingSignal(
            instrument=self.instrument,
            decision=decision,
            bucket_start=latest.bucket_start,
            payload=build_signal_payload(decision, self.instrument, settings),
        )
