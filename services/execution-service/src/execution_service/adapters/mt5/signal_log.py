"""The MT5 adapter's second JSONL log: one line per signal.

Separate from the events log because it is a domain record, not diagnostics.
It was `SignalFileLog` in mt5-trader, wrapping the same writer ta-core now owns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ta_core import JsonlLogger


class SignalFileLog:
    def __init__(self, path: Path) -> None:
        self._logger = JsonlLogger(path)

    def append(self, record: dict[str, Any]) -> None:
        self._logger.append(record)
