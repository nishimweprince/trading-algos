"""Incremental indicators that reproduce the research's pandas formulas (lib.py) value for value.

`Ewm` is a line-for-line port of pandas' `ewm(..., adjust=False, ignore_na=False).mean()` recursion, including how it
treats NaN inputs, so EMA / Wilder ATR / RSI / ADX match `lib.py` to floating-point precision.
"""

from __future__ import annotations

import math
from collections import deque

NAN = float("nan")


def _isnan(x: float) -> bool:
    return x != x


class Ewm:
    """pandas ewm(alpha=..., adjust=False, ignore_na=False).mean(), fed one value at a time."""

    __slots__ = ("alpha", "old_wt_factor", "weighted", "old_wt", "started", "value")

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.old_wt_factor = 1.0 - alpha
        self.weighted = NAN
        self.old_wt = 1.0
        self.started = False
        self.value = NAN

    @classmethod
    def span(cls, n: int) -> Ewm:
        return cls(2.0 / (n + 1.0))

    @classmethod
    def wilder(cls, n: int) -> Ewm:
        return cls(1.0 / n)

    def update(self, cur: float) -> float:
        if not self.started:
            self.started = True
            self.weighted = cur
            self.old_wt = 1.0
            self.value = cur
            return cur
        is_obs = not _isnan(cur)
        w = self.weighted
        if not _isnan(w):
            self.old_wt *= self.old_wt_factor
            if is_obs:
                if w != cur:
                    self.weighted = (self.old_wt * w + self.alpha * cur) / (self.old_wt + self.alpha)
                self.old_wt = 1.0
        elif is_obs:
            self.weighted = cur
        self.value = self.weighted
        return self.value


class Atr:
    """Wilder ATR(n) on bars (lib.atr)."""

    __slots__ = ("ewm", "prev_c", "value")

    def __init__(self, n: int = 14):
        self.ewm = Ewm.wilder(n)
        self.prev_c = NAN
        self.value = NAN

    def update(self, h: float, l: float, c: float) -> float:
        pc = self.prev_c
        if _isnan(pc):
            tr = h - l
        else:
            tr = max(h - l, abs(h - pc), abs(l - pc))
        self.prev_c = c
        self.value = self.ewm.update(tr)
        return self.value


class Rsi:
    """Wilder RSI(n) (lib.rsi): NaN when the down average is 0."""

    __slots__ = ("up", "dn", "prev", "value")

    def __init__(self, n: int):
        self.up, self.dn = Ewm.wilder(n), Ewm.wilder(n)
        self.prev = NAN
        self.value = NAN

    def update(self, c: float) -> float:
        d = c - self.prev if not _isnan(self.prev) else NAN
        self.prev = c
        if _isnan(d):
            u = self.up.update(NAN)
            dn = self.dn.update(NAN)
        else:
            u = self.up.update(d if d > 0 else 0.0)
            dn = self.dn.update(-d if d < 0 else 0.0)
        if _isnan(u) or _isnan(dn) or dn == 0:
            self.value = NAN
        else:
            self.value = 100 - 100 / (1 + u / dn)
        return self.value


class Adx:
    """ADX(n) (lib.adx): Wilder +DM/-DM over Wilder ATR."""

    __slots__ = ("atr", "pdm", "ndm", "dx", "ph", "pl", "value")

    def __init__(self, n: int = 14):
        self.atr = Atr(n)
        self.pdm, self.ndm, self.dx = Ewm.wilder(n), Ewm.wilder(n), Ewm.wilder(n)
        self.ph = NAN
        self.pl = NAN
        self.value = NAN

    def update(self, h: float, l: float, c: float) -> float:
        up = h - self.ph if not _isnan(self.ph) else NAN
        dn = self.pl - l if not _isnan(self.pl) else NAN
        self.ph, self.pl = h, l
        # np.where((up > dn) & (up > 0), up, 0.0): comparisons with NaN are False -> 0.0
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        a = self.atr.update(h, l, c)
        sp, sn = self.pdm.update(pdm), self.ndm.update(ndm)
        with_nan = _isnan(a) or a == 0
        pdi = NAN if with_nan else 100 * sp / a
        ndi = NAN if with_nan else 100 * sn / a
        den = pdi + ndi
        if _isnan(den) or den == 0:
            dx = NAN
        else:
            dx = 100 * abs(pdi - ndi) / den
        self.value = self.dx.update(dx)
        return self.value


class Lagged:
    """Keeps the last n+1 values so `ago(k)` is pandas `shift(k)` (NaN until available)."""

    __slots__ = ("buf",)

    def __init__(self, n: int):
        self.buf: deque[float] = deque(maxlen=n + 1)

    def push(self, x: float) -> None:
        self.buf.append(x)

    def ago(self, k: int) -> float:
        return self.buf[-1 - k] if len(self.buf) > k else NAN


def isfinite(x: float) -> bool:
    return not (_isnan(x) or math.isinf(x))
