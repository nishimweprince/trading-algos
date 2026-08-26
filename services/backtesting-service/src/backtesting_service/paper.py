"""Closed-bar paper loop. Persists engine state across restarts.

Orders reach a broker only when an ExecutionBridge is attached; without one this stays
exactly what it was, a simulation.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .candles import CandleStore
from .config import Settings
from .engine import ClosedBarEngine
from .execution_bridge import ExecutionBridge
from .logging_config import log_event
from .models import TIMEFRAME_MINUTES, Candle, EngineEvent, PaperExecutionObservation, PaperStatus
from .notifier import Notifier

_OBSERVATION_KINDS = frozenset({"entry", "partial_tp", "exit"})


class PaperTrader:
    def __init__(
        self,
        settings: Settings,
        store: CandleStore,
        engine: ClosedBarEngine,
        notifier: Notifier,
        state_path: Path,
        bridge: ExecutionBridge | None = None,
    ) -> None:
        self._s = settings
        self._store = store
        self.engine = engine
        self._notifier = notifier
        self._state_path = state_path
        self.bridge = bridge
        self.last_ts: datetime | None = None
        self.execution_observations: list[PaperExecutionObservation] = []

    def load(self) -> None:
        if not self._state_path.is_file():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        last = payload.get("last_ts")
        if last:
            self.last_ts = datetime.fromisoformat(str(last))
        snapshot = payload.get("engine")
        if isinstance(snapshot, dict):
            self.engine.restore(snapshot)
        execution = payload.get("execution")
        if isinstance(execution, dict) and self.bridge is not None:
            self.bridge.restore(execution)
        observations = payload.get("execution_observations")
        if isinstance(observations, list):
            self.execution_observations = [
                PaperExecutionObservation.model_validate(item)
                for item in observations
                if isinstance(item, dict)
            ]

    def save(self) -> None:
        """Persist state atomically.

        Written to a sibling temp file and renamed, because with real orders resting at a
        broker a crash midway through a plain write loses the order ids needed to cancel
        them, and an orphaned position is far worse than a lost tick.
        """
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schema_version": 1,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "engine": self.engine.snapshot(),
            "execution_observations": [
                item.model_dump(mode="json") for item in self.execution_observations
            ],
        }
        if self.bridge is not None:
            payload["execution"] = self.bridge.snapshot()
        tmp = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def status(self) -> PaperStatus:
        return PaperStatus(
            enabled=self._s.paper_enabled,
            last_ts=self.last_ts,
            open_pairs=self.engine.open_pair_views(),
            pending_entry_orders=self.engine.open_entry_order_views(),
            stats=self.engine.stats,
            events=self.engine.events[-50:],
            prop_guard_breached=self.engine.prop_guard.state.breached,
            prop_guard_breach_reason=self.engine.prop_guard.state.breach_reason,
            execution_observations=self.execution_observations[-50:],
            trade_pairs=self.engine.closed_trade_pairs(),
            equity_curve=self.engine.equity_curve_points(),
            execution_mode=self._s.market_execution_mode,
            sends_broker_orders=(self.bridge is not None and self.bridge.mode.sends_orders),
        )

    def _bar_step(self) -> timedelta:
        return timedelta(minutes=TIMEFRAME_MINUTES[self._s.timeframe])

    def _warn_paper_data_quality(self, candles: list[Candle], new: list[Candle]) -> None:
        for previous, bar in zip(candles, candles[1:], strict=False):
            if bar.ts <= previous.ts:
                log_event(
                    "paper_bars_out_of_order",
                    previous_ts=previous.ts.isoformat(),
                    bar_ts=bar.ts.isoformat(),
                )
                break
        if self.last_ts is None or not new:
            return
        replayed = next((bar for bar in candles if bar.ts == self.last_ts), None)
        if (
            replayed is not None
            and self.engine.last_bar is not None
            and self.engine.last_bar.ts == self.last_ts
            and (
                replayed.open,
                replayed.high,
                replayed.low,
                replayed.close,
            )
            != (
                self.engine.last_bar.open,
                self.engine.last_bar.high,
                self.engine.last_bar.low,
                self.engine.last_bar.close,
            )
        ):
            log_event(
                "paper_bar_corrected",
                bar_ts=self.last_ts.isoformat(),
                previous_close=self.engine.last_bar.close,
                corrected_close=replayed.close,
            )
        expected = self.last_ts + self._bar_step()
        if new[0].ts > expected:
            dropped = sum(1 for bar in candles if bar.ts <= self.last_ts)
            log_event(
                "paper_gap_detected",
                last_ts=self.last_ts.isoformat(),
                expected_ts=expected.isoformat(),
                got_ts=new[0].ts.isoformat(),
                lookback_bars_at_or_before_last_ts=dropped,
            )

    def _record_observation(self, event: EngineEvent, bar: Candle) -> None:
        if event.kind not in _OBSERVATION_KINDS:
            return
        fill = event.detail.get("entry")
        if fill is None:
            fill = event.detail.get("fill")
        if fill is None:
            fill = event.detail.get("price")
        observation = PaperExecutionObservation(
            observed_at=datetime.now(tz=UTC),
            bar_ts=bar.ts,
            event_kind=event.kind,
            session=event.session,
            fill_price=float(fill) if fill is not None else None,
            modeled_slippage_pips_per_side=self.engine.params.slippage_pips_per_side,
            pair_id=str(event.detail["pair_id"]) if event.detail.get("pair_id") else None,
        )
        self.execution_observations.append(observation)
        overflow = len(self.execution_observations) - self._s.paper_event_retention
        if overflow > 0:
            del self.execution_observations[:overflow]

    def _prune(self) -> None:
        self.engine.prune_closed_history(
            max_closed_pairs=self._s.paper_closed_pair_retention,
            max_events=self._s.paper_event_retention,
            max_trades=self._s.paper_trade_retention,
            max_bars=self._s.paper_bar_retention,
        )

    async def tick(self) -> None:
        candles = await self._store.fetch_ctrader(
            self._s.symbol,
            self._s.timeframe,
            count=self._s.paper_lookback,
        )
        if not candles:
            return
        if self.last_ts is None:
            self.engine.observe(candles[-1])
            self.last_ts = candles[-1].ts
            self.save()
            log_event("paper_warmed", last_ts=self.last_ts.isoformat(), bars=len(candles))
            return
        new = [bar for bar in candles if bar.ts > self.last_ts]
        self._warn_paper_data_quality(candles, new)
        if not new:
            return
        for bar in new:
            events = self.engine.step(bar)
            self.last_ts = bar.ts
            for event in events:
                log_event(event.kind, session=event.session, **event.detail)
                self._record_observation(event, bar)
                if self.bridge is not None:
                    await self.bridge.handle(event, bar)
                await self._notify(event)
        self._prune()
        self.save()

    async def _notify(self, event: EngineEvent) -> None:
        if event.kind not in {"entry", "lock", "exit"}:
            return
        subject = f"session-hedge {event.kind} {event.session}"
        lines = [f"{event.kind} {event.session} @ {event.ts.isoformat()}", str(event.detail)]
        await self._notifier.send(subject, lines, kind=event.kind, session=event.session)
