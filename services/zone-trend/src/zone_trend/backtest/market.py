"""Market data the simulated broker reads: 1m last and mark klines, funding.

1m data is loaded a month at a time and only for symbols the broker asks
about (open positions and pending orders), so a multi-year, multi-symbol run
never holds more than a few months of minutes in memory.

Where a mark minute is missing (Vision lacks whole days of mark klines), the
last-price minute stands in for it; the run counts how often that happened.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from zone_trend.data.store import EVENT_TF, DataPaths, load

__all__ = ["Funding", "MinuteBars", "MinuteSource"]

CACHE_MONTHS = 12


@dataclass(frozen=True)
class MinuteBars:
    """One entry-bar span (or less) of minutes for one symbol."""

    open_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    mark_high: np.ndarray
    mark_low: np.ndarray
    mark_close: np.ndarray
    mark_missing: int

    def __len__(self) -> int:
        return len(self.open_time)


def _month(ts: int) -> str:
    return pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m")


class MinuteSource:
    def __init__(self, paths: DataPaths):
        self.paths = paths
        self._cache: OrderedDict[tuple[str, str], tuple] = OrderedDict()
        self.mark_missing = 0

    def _month_arrays(self, symbol: str, month: str):
        key = (symbol, month)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        start = int(pd.Timestamp(month + "-01", tz="UTC").value // 1_000_000)
        end = int((pd.Timestamp(month + "-01", tz="UTC") + pd.offsets.MonthBegin()).value // 1e6)
        cols = ["open_time", "open", "high", "low", "close"]
        last = load(self.paths, "klines", symbol, "1m", start, end, columns=cols)
        mark = load(self.paths, "markPriceKlines", symbol, "1m", start, end, columns=cols)
        t = last["open_time"].to_numpy(np.int64)
        arrays = [t] + [last[c].to_numpy(float) for c in ("open", "high", "low", "close")]
        if len(mark):
            m = mark.set_index("open_time").reindex(t)
            missing = m["close"].isna().to_numpy()
            mh = np.where(missing, arrays[2], m["high"].to_numpy(float))
            ml = np.where(missing, arrays[3], m["low"].to_numpy(float))
            mc = np.where(missing, arrays[4], m["close"].to_numpy(float))
        else:
            missing = np.ones(len(t), bool)
            mh, ml, mc = arrays[2], arrays[3], arrays[4]
        value = (*arrays, mh, ml, mc, missing)
        self._cache[key] = value
        if len(self._cache) > CACHE_MONTHS:
            self._cache.popitem(last=False)
        return value

    def window(self, symbol: str, start: int, span_ms: int) -> MinuteBars:
        arr = self._month_arrays(symbol, _month(start))
        t = arr[0]
        i, j = np.searchsorted(t, [start, start + span_ms])
        missing = int(arr[8][i:j].sum())
        self.mark_missing += missing
        return MinuteBars(*(a[i:j] for a in arr[:8]), missing)


class Funding:
    """Settled funding per symbol: settlement times and 8h-normalised rates."""

    def __init__(self, paths: DataPaths, symbols: list[str]):
        self._t: dict[str, np.ndarray] = {}
        self._rate: dict[str, np.ndarray] = {}
        self._rate8h: dict[str, np.ndarray] = {}
        for s in symbols:
            df = load(paths, "fundingRate", s, EVENT_TF)
            if df.empty:
                continue
            self._t[s] = df["settle_time"].to_numpy(np.int64)
            self._rate[s] = df["last_funding_rate"].to_numpy(float)
            hours = df["funding_interval_hours"].to_numpy(float)
            self._rate8h[s] = self._rate[s] * 8 / np.where(hours > 0, hours, 8)

    def settlements(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        """(time, rate) for settlements in [start, end)."""
        t = self._t.get(symbol)
        if t is None:
            return []
        i, j = np.searchsorted(t, [start, end])
        return [(int(t[k]), float(self._rate[symbol][k])) for k in range(i, j)]

    def last_8h(self, symbol: str, at: int) -> float | None:
        """The last settled rate at or before ``at``, normalised to 8 hours."""
        t = self._t.get(symbol)
        if t is None:
            return None
        i = int(np.searchsorted(t, at, side="right")) - 1
        return float(self._rate8h[symbol][i]) if i >= 0 else None
