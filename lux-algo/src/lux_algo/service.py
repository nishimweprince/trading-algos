"""Orchestrates one poll cycle: fetch 1M candles -> aggregate -> evaluate the LuxAlgo
Supertrend entry on the forming bar -> gate (fire once per candle) -> submit to
mt5-trader.
"""

from __future__ import annotations

import logging

from .candles import Aggregator
from .config import Settings
from .data_client import MarketDataClient
from .logging_config import RuntimeLogs, log_event
from .models import build_signal_payload
from .mt5_client import Mt5TraderClient
from .signal_gate import SignalGate
from .strategy import StrategyParams, SupertrendSignalStrategy


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
        self._strategy = SupertrendSignalStrategy(
            StrategyParams(
                sensitivity=settings.supertrend_sensitivity,
                atr_len=settings.supertrend_atr_len,
                sma_len=settings.sma_len,
                risk_reward=settings.risk_reward,
                send_stop_loss=settings.send_stop_loss,
                send_take_profit=settings.send_take_profit,
                use_hard_targets=settings.use_hard_targets,
                hard_stop_loss_usd=settings.hard_stop_loss_usd,
                hard_take_profit_usd=settings.hard_take_profit_usd,
                volume=float(settings.volume),
                contract_size=settings.contract_size,
                confluence_mode=settings.confluence_mode,
                confluence_threshold=settings.confluence_threshold,
                enabled_overlays=settings.enabled_overlays,
                veto_tp_points=settings.veto_tp_points,
                veto_reversals=settings.veto_reversals,
            )
        )
        self._gate = SignalGate()

    async def tick(self) -> None:
        try:
            minutes = await self._data.fetch_minute_candles()
        except Exception as exc:  # noqa: BLE001 - poll failure must not kill the loop
            self._logs.errors.append({"kind": "data_poll_failed", "error": str(exc)})
            log_event("data_poll_failed", level=logging.ERROR, error=str(exc), exc_info=True)
            return

        series = self._aggregator.build(minutes)
        if series.forming is None:
            return

        bucket_start = series.forming.start
        if self._gate.is_locked(bucket_start):
            return

        decision = self._strategy.evaluate(series)
        if decision is None:
            return

        # Lock immediately: fire once per target candle regardless of what happens next.
        self._gate.lock(bucket_start)
        payload = build_signal_payload(decision, self._s)
        self._logs.signals.append(
            {
                "kind": "signal_fired",
                "direction": decision.direction,
                "bucket_start": bucket_start.isoformat(),
                "entry": decision.entry,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "supertrend": decision.supertrend,
                "confluence": decision.confluence,
                "signal_id": payload["signal_id"],
            }
        )
        log_event(
            "signal_fired",
            direction=decision.direction,
            bucket_start=bucket_start.isoformat(),
            signal_id=payload["signal_id"],
            entry=decision.entry,
        )

        outcome = await self._mt5.submit(payload)
        self._logs.executions.append(
            {
                "signal_id": payload["signal_id"],
                "outcome": outcome.kind,
                "status_code": outcome.status_code,
                "detail": outcome.detail,
            }
        )
        level = logging.INFO if outcome.kind == "success" else logging.WARNING
        log_event(
            "signal_submitted",
            level=level,
            signal_id=payload["signal_id"],
            outcome=outcome.kind,
            status_code=outcome.status_code,
        )
        if outcome.kind == "unauthorized":
            # Bad key is a configuration fault; keep it loud but do not crash the loop.
            log_event("mt5_unauthorized", level=logging.ERROR, signal_id=payload["signal_id"])
