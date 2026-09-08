"""Orchestrates one poll cycle: fetch 1M candles -> aggregate -> evaluate the LuxAlgo
Supertrend entry on the forming bar -> gate (fire once per candle) -> submit to
mt5-trader.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .candles import Aggregator
from .config import Settings
from .data_client import MarketDataClient
from .instruments import InstrumentConfig
from .logging_config import RuntimeLogs, log_event
from .models import build_signal_payload
from .mt5_client import Mt5TraderClient
from .signal_gate import SignalGate
from .strategy import Decision, StrategyParams, SupertrendSignalStrategy


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
        self._strategy = SupertrendSignalStrategy(
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
                confluence_mode=settings.confluence_mode,
                confluence_threshold=settings.confluence_threshold,
                enabled_overlays=settings.enabled_overlays,
                veto_tp_points=settings.veto_tp_points,
                veto_reversals=settings.veto_reversals,
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
    ) -> None:
        self._s = settings
        self._data = data_client
        self._mt5 = mt5_client
        self._logs = logs
        self._aggregator = Aggregator(settings.target_tf_minutes, settings.bucket_offset_minutes)
        self._pipelines = [
            _InstrumentPipeline(instrument, settings) for instrument in settings.instruments
        ]

    async def tick(self) -> None:
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

        for signal in pending:
            self._logs.signals.append(
                {
                    "kind": "signal_fired",
                    "quote": signal.instrument.quote,
                    "symbol": signal.instrument.resolved_mt5_symbol(),
                    "direction": signal.decision.direction,
                    "bucket_start": signal.bucket_start.isoformat(),
                    "entry": signal.decision.entry,
                    "stop_loss": signal.decision.stop_loss,
                    "take_profit": signal.decision.take_profit,
                    "supertrend": signal.decision.supertrend,
                    "confluence": signal.decision.confluence,
                    "signal_id": signal.payload["signal_id"],
                }
            )
            log_event(
                "signal_fired",
                quote=signal.instrument.quote,
                symbol=signal.instrument.resolved_mt5_symbol(),
                direction=signal.decision.direction,
                bucket_start=signal.bucket_start.isoformat(),
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

    async def _fetch_for_pipeline(self, pipeline: _InstrumentPipeline) -> Any:
        minutes = await self._data.fetch_minute_candles(pipeline.instrument.quote)
        return self._aggregator.build(minutes)
