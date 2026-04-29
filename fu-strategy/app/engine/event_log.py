"""Append-only JSONL writer for indicator/structure/zone/signal events.

Used to replay detections offline against TradingView screenshots so we can
verify Python parity with the Pine reference indicators.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from app.core.types import FUEvent, Signal, StructureEvent, ZoneEvent, ensure_utc, utc_now


_lock = threading.Lock()


def _ts(value: Optional[datetime]) -> str:
    return ensure_utc(value or utc_now()).isoformat()


class EventLog:
    """Thread-safe JSONL appender. One instance per process is enough."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(',', ':'), default=str)
        with _lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def log(self, symbol: str, timeframe: str, kind: str, data: Dict[str, Any],
            ts: Optional[datetime] = None) -> None:
        self._write({
            'ts': _ts(ts),
            'symbol': symbol,
            'timeframe': timeframe,
            'kind': kind,
            'data': data,
        })

    def log_fu(self, symbol: str, timeframe: str, event: FUEvent) -> None:
        self.log(symbol, timeframe, 'fu', event.to_log(), ts=event.time)

    def log_structure(self, symbol: str, timeframe: str, event: StructureEvent) -> None:
        self.log(symbol, timeframe, 'structure', event.to_log(), ts=event.time)

    def log_zone(self, symbol: str, timeframe: str, event: ZoneEvent) -> None:
        self.log(symbol, timeframe, 'zone', event.to_log(), ts=event.time)

    def log_signal(self, signal: Signal) -> None:
        self.log(signal.symbol, signal.timeframe, 'signal', signal.to_log(),
                 ts=signal.created_at)

    def log_bias_change(self, symbol: str, timeframe: str, old_bias: str,
                        new_bias: str, ts: Optional[datetime] = None) -> None:
        self.log(symbol, timeframe, 'bias_change',
                 {'from': old_bias, 'to': new_bias}, ts=ts)


_default: Optional[EventLog] = None


def get_event_log(path: Optional[str] = None) -> EventLog:
    global _default
    if _default is None or (path and str(_default.path) != path):
        _default = EventLog(path or os.environ.get(
            'INDICATOR_EVENT_LOG_PATH', './logs/indicator_events.jsonl'))
    return _default
