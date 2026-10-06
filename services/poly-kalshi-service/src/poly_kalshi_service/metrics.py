"""Rolling latency and timing metrics, cheap on the hot path.

``add`` appends to a bounded deque (no sorting, no allocation beyond the
sample). Percentiles are computed only in ``snapshot``, which runs when
status is read (once per 5 s by ``pk-status``), never per message.

Metric names (all in the unit in their suffix):

- ``feed_ms_p`` / ``feed_ms_k``: local receive time minus the venue's own
  message timestamp. Includes clock skew between hosts; keep chrony on.
- ``engine_us``: time to handle one line (parse, book update, decision).
- ``loop_lag_ms``: event-loop delay past a 0.5 s sleep; work queued behind
  the feeds shows up here first.
- ``age_ms_kalshi`` / ``age_ms_poly``: how old each leg's book was when a
  window-consuming decision fired. A stale Kalshi leg means a stale signal.
- ``resolution_s_poly`` / ``resolution_s_kalshi``: seconds after the window
  closed that each venue's result arrived.
"""

from __future__ import annotations

from collections import deque
from typing import Any

__all__ = ["Metrics"]

WINDOW = 4096


def _pct(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


class Metrics:
    def __init__(self, window: int = WINDOW) -> None:
        self._window = window
        self._samples: dict[str, deque[float]] = {}
        self._counts: dict[str, int] = {}

    def add(self, name: str, value: float) -> None:
        samples = self._samples.get(name)
        if samples is None:
            samples = self._samples[name] = deque(maxlen=self._window)
            self._counts[name] = 0
        samples.append(value)
        self._counts[name] += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, samples in self._samples.items():
            if not samples:
                continue
            ordered = sorted(samples)
            out[name] = {
                "n": self._counts[name],
                "window": len(ordered),
                "p50": round(_pct(ordered, 0.50), 3),
                "p90": round(_pct(ordered, 0.90), 3),
                "p99": round(_pct(ordered, 0.99), 3),
                "max": round(ordered[-1], 3),
            }
        return out
