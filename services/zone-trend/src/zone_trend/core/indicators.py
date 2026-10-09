"""Indicators, each with an incremental form (used by the core) and a vectorised
reference (used by tests; the two must agree within 1e-9).

Seeding conventions, fixed so live and backtest agree byte for byte:

- ATR (Wilder): TR of the first bar is high - low; the first ATR is the mean of
  the first ``n`` TRs; then ``atr = (atr * (n - 1) + tr) / n``. Undefined (nan)
  before ``n`` bars.
- EMA: the first value is the SMA of the first ``n`` closes; then
  ``ema += alpha * (x - ema)`` with ``alpha = 2 / (n + 1)``. nan before ``n``.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

__all__ = [
    "Ema",
    "RollingMedian",
    "Wilder",
    "atr_series",
    "ema_series",
    "pearson",
    "volume_profile_hvn",
]

NAN = float("nan")


class Wilder:
    """Incremental Wilder ATR."""

    __slots__ = ("n", "value", "_prev_close", "_seed")

    def __init__(self, n: int = 14):
        self.n = n
        self.value = NAN
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            pc = self._prev_close
            tr = max(high - low, abs(high - pc), abs(low - pc))
        self._prev_close = close
        if len(self._seed) < self.n:
            self._seed.append(tr)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
            return self.value
        self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


def atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    tr[0] = high[0] - low[0]
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


class Ema:
    __slots__ = ("n", "alpha", "value", "_seed")

    def __init__(self, n: int):
        self.n = n
        self.alpha = 2.0 / (n + 1)
        self.value = NAN
        self._seed: list[float] = []

    def update(self, x: float) -> float:
        if len(self._seed) < self.n:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


def ema_series(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = out[i - 1] + alpha * (x[i] - out[i - 1])
    return out


class RollingMedian:
    """Median of the last ``n`` values (nan until ``n`` values have been seen)."""

    __slots__ = ("n", "_buf")

    def __init__(self, n: int):
        self.n = n
        self._buf: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> float:
        self._buf.append(x)
        return self.value

    @property
    def value(self) -> float:
        if len(self._buf) < self.n:
            return NAN
        return float(np.median(self._buf))


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3 or len(a) != len(b):
        return NAN
    sa, sb = a.std(), b.std()
    if sa == 0 or sb == 0:
        return NAN
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def volume_profile_hvn(
    high: np.ndarray, low: np.ndarray, volume: np.ndarray, bins: int = 50, pct: float = 0.75
) -> list[float]:
    """High-volume nodes: centres of local-peak bins at or above the ``pct`` quantile.

    Ported from ``vrvp-strategy/indicators/volume_profile.py`` (each bar's
    volume spread evenly over the bins its range covers), vectorised, and
    reduced to local peaks so a fat node counts once. The caller passes only a
    past window.
    """
    high, low, volume = (np.asarray(a, dtype=float) for a in (high, low, volume))
    if len(high) == 0:
        return []
    lo, hi = float(low.min()), float(high.max())
    if not math.isfinite(lo) or hi <= lo:
        return []
    edges = np.linspace(lo, hi, bins + 1)
    width = edges[1] - edges[0]
    first = np.clip(((low - lo) / width).astype(int), 0, bins - 1)
    last = np.clip(((high - lo) / width).astype(int), 0, bins - 1)
    span = last - first + 1
    per_bin = volume / span
    profile = np.zeros(bins + 1)
    np.add.at(profile, first, per_bin)
    np.add.at(profile, last + 1, -per_bin)
    profile = np.cumsum(profile)[:bins]
    if profile.max() <= 0:
        return []
    cut = np.quantile(profile, pct)
    padded = np.concatenate([[-np.inf], profile, [-np.inf]])
    peaks = (profile >= cut) & (profile >= padded[:-2]) & (profile > padded[2:])
    centres = (edges[:-1] + edges[1:]) / 2
    return [float(c) for c in centres[peaks]]
