"""Confirmed swing pivots.

A swing high at bar ``i`` is confirmed at bar ``i + R`` when

- ``H[i]`` is strictly above each of the previous ``L`` highs,
- ``H[i]`` is at or above each of the next ``R`` highs, and
- its leg is at least ``k * ATR14`` (ATR at bar ``i``).

The leg runs from the last kept swing's pivot (or the start of the ``L``
window when there is none) to ``i``: for a high, ``H[i]`` minus the lowest low
in that span. Lows mirror this.

Kept swings alternate high/low. When a new swing has the same kind as the last
kept one, the more extreme of the two survives; a replacement takes the later
``t_conf``, because downstream logic could not have known about it earlier.

``t_conf`` is the close time of bar ``i + R``; nothing may read a swing before
then.
"""

from __future__ import annotations

import math
from dataclasses import replace

from zone_trend.core.series import BarSeries
from zone_trend.types import Swing

__all__ = ["SwingTracker"]


class SwingTracker:
    def __init__(self, left: int, right: int, k_atr: float):
        self.left = left
        self.right = right
        self.k = k_atr
        self.swings: list[Swing] = []
        self._last_pivot_idx: int | None = None

    def update(self, s: BarSeries) -> list[Swing]:
        """Process the newest bar of ``s``; return swings confirmed or replaced by it."""
        i = len(s) - 1 - self.right
        if i < self.left:
            return []
        bars = s.bars
        atr = s.atr[i]
        if not math.isfinite(atr):
            return []
        piv = bars[i]
        t_conf = bars[-1].close_time
        start = self._last_pivot_idx if self._last_pivot_idx is not None else i - self.left
        window = bars[start : i + 1]
        out: list[Swing] = []

        before = bars[i - self.left : i]
        after = bars[i + 1 : i + 1 + self.right]
        body = abs(piv.close - piv.open)
        if all(piv.high > b.high for b in before) and all(piv.high >= b.high for b in after):
            leg = piv.high - min(b.low for b in window)
            if leg >= self.k * atr:
                sw = Swing(s.tf, "high", piv.open_time, piv.high, t_conf, atr, body)
                if self._keep(sw, i):
                    out.append(self.swings[-1])
        if all(piv.low < b.low for b in before) and all(piv.low <= b.low for b in after):
            leg = max(b.high for b in window) - piv.low
            if leg >= self.k * atr:
                sw = Swing(s.tf, "low", piv.open_time, piv.low, t_conf, atr, body)
                if self._keep(sw, i):
                    out.append(self.swings[-1])
        return out

    def _keep(self, sw: Swing, idx: int) -> bool:
        if self.swings and self.swings[-1].kind == sw.kind:
            prev = self.swings[-1]
            more_extreme = sw.price > prev.price if sw.kind == "high" else sw.price < prev.price
            if not more_extreme:
                return False
            self.swings[-1] = replace(sw, t_conf=max(sw.t_conf, prev.t_conf))
        else:
            self.swings.append(sw)
        self._last_pivot_idx = idx
        return True

    def visible(self, now: int) -> list[Swing]:
        """Swings whose confirmation time is at or before ``now``."""
        return [sw for sw in self.swings if sw.t_conf <= now]
