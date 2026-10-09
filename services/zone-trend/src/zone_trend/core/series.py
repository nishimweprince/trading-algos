"""Closed bars of one timeframe plus the indicators every rule reads.

The core keeps the whole history it has been fed (a backtest year of 1H is
~9k bars) rather than a ring buffer; the live runner seeds it from a REST
backfill of the spec's lengths, so the two see the same recent window once
warm. Appending is the only mutation.
"""

from __future__ import annotations

from zone_trend.core.indicators import Ema, RollingMedian, Wilder
from zone_trend.types import TF, Bar

__all__ = ["BarSeries"]


class BarSeries:
    def __init__(self, tf: TF, ema_period: int = 50, atr_period: int = 14, vol_median: int = 20):
        self.tf = tf
        self.bars: list[Bar] = []
        self.atr: list[float] = []
        self.ema: list[float] = []
        self.vol_med: list[float] = []  # median of the previous ``vol_median`` volumes
        self._atr = Wilder(atr_period)
        self._ema = Ema(ema_period)
        self._vol = RollingMedian(vol_median)

    def append(self, bar: Bar) -> None:
        if self.bars and bar.open_time <= self.bars[-1].open_time:
            raise ValueError(
                f"{self.tf}: bar {bar.open_time} is not after {self.bars[-1].open_time}"
            )
        # The volume median excludes the bar itself, so a breakout bar is compared
        # with the 20 bars before it.
        self.vol_med.append(self._vol.value)
        self._vol.update(bar.volume)
        self.bars.append(bar)
        self.atr.append(self._atr.update(bar.high, bar.low, bar.close))
        self.ema.append(self._ema.update(bar.close))

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def last(self) -> Bar:
        return self.bars[-1]

    @property
    def last_atr(self) -> float:
        return self.atr[-1]

    def close_time(self) -> int:
        return self.bars[-1].close_time if self.bars else -1
