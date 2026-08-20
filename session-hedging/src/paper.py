"""Closed-bar paper loop. No orders. Persists engine state across restarts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from candles import CandleStore
from config import Settings
from engine import ClosedBarEngine
from logging_config import log_event
from models import EngineEvent, PaperStatus
from notifier import Notifier


class PaperTrader:
    def __init__(
        self,
        settings: Settings,
        store: CandleStore,
        engine: ClosedBarEngine,
        notifier: Notifier,
        state_path: Path,
    ) -> None:
        self._s = settings
        self._store = store
        self.engine = engine
        self._notifier = notifier
        self._state_path = state_path
        self.last_ts: datetime | None = None

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

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "engine": self.engine.snapshot(),
        }
        self._state_path.write_text(json.dumps(payload, default=str), encoding="utf-8")

    def status(self) -> PaperStatus:
        return PaperStatus(
            enabled=self._s.paper_enabled,
            last_ts=self.last_ts,
            open_pairs=self.engine.open_pair_views(),
            stats=self.engine.stats,
            events=self.engine.events[-50:],
            prop_guard_breached=self.engine.prop_guard.state.breached,
            prop_guard_breach_reason=self.engine.prop_guard.state.breach_reason,
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
        if not new:
            return
        for bar in new:
            events = self.engine.step(bar)
            self.last_ts = bar.ts
            for event in events:
                log_event(event.kind, session=event.session, **event.detail)
                await self._notify(event)
        self.save()

    async def _notify(self, event: EngineEvent) -> None:
        if event.kind not in {"entry", "lock", "exit"}:
            return
        subject = f"session-hedge {event.kind} {event.session}"
        lines = [f"{event.kind} {event.session} @ {event.ts.isoformat()}", str(event.detail)]
        await self._notifier.send(subject, lines, kind=event.kind, session=event.session)
