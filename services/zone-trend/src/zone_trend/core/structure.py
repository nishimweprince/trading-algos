"""Trend state, break of structure / change of character, and leg slopes.

Trend (evaluated on each close of its timeframe):

- Up: the last two swing highs are higher highs, the last two lows are higher
  lows, close above EMA50, and the confirming (higher) timeframe is not Down.
- Down: the mirror. Range: anything else.

The 1D state is computed by the same rule with no confirming timeframe.

CHoCH: in Up, a close below the last higher low forces Range until a new swing
is confirmed after that close; Down mirrors. BOS: a close beyond the last swing
extreme in the trend direction (reported as an event; it does not change state).

Slopes (the sketch's dPrice/dt) are normalised so legs compare across symbols
and volatility regimes::

    slope = ((P_end - P_start) / P_start / bars) * (bars_per_day / (ATR14_1D / C))

A leg is the move between consecutive kept swings. For a long, impulse legs
are the up legs and corrections the down legs; shorts mirror.

Momentum decay: the latest impulse's |slope| is below ``decay_ratio`` times the
median |slope| of the three impulses before it. (The spec says "the last three
impulses"; excluding the latest keeps it from diluting its own baseline.)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from zone_trend.types import TF, Side, Swing, Trend

__all__ = [
    "Leg",
    "StructureState",
    "impulse_and_correction",
    "legs",
    "momentum_decay",
    "slope_ok",
    "trend_rule",
]


def trend_rule(swings: list[Swing], close: float, ema: float, higher: Trend | None) -> Trend:
    highs = [s.price for s in swings if s.kind == "high"][-2:]
    lows = [s.price for s in swings if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2 or not math.isfinite(ema):
        return Trend.RANGE
    if highs[1] > highs[0] and lows[1] > lows[0] and close > ema and higher != Trend.DOWN:
        return Trend.UP
    if highs[1] < highs[0] and lows[1] < lows[0] and close < ema and higher != Trend.UP:
        return Trend.DOWN
    return Trend.RANGE


@dataclass
class StructureState:
    """Trend for one timeframe, with the CHoCH override and BOS/CHoCH events."""

    tf: TF
    trend: Trend = Trend.RANGE
    choch_at: int | None = None  # close time of the CHoCH close; cleared by a newer swing
    choch_side: Trend | None = None  # the trend the CHoCH broke
    events: list[tuple[int, str]] = field(default_factory=list)  # (time, "bos_up" | ...)

    def update(
        self, swings: list[Swing], close: float, close_time: int, ema: float, higher: Trend | None
    ) -> Trend:
        base = trend_rule(swings, close, ema, higher)
        if self.choch_at is not None and swings and swings[-1].t_conf > self.choch_at:
            self.choch_at = None
            self.choch_side = None
        lows = [s for s in swings if s.kind == "low"]
        highs = [s for s in swings if s.kind == "high"]
        if self.choch_at is None and base == Trend.UP and lows and close < lows[-1].price:
            self.choch_at, self.choch_side = close_time, Trend.UP
            self.events.append((close_time, "choch_down"))
        elif self.choch_at is None and base == Trend.DOWN and highs and close > highs[-1].price:
            self.choch_at, self.choch_side = close_time, Trend.DOWN
            self.events.append((close_time, "choch_up"))
        if base == Trend.UP and highs and close > highs[-1].price:
            self.events.append((close_time, "bos_up"))
        elif base == Trend.DOWN and lows and close < lows[-1].price:
            self.events.append((close_time, "bos_down"))
        self.trend = Trend.RANGE if self.choch_at is not None else base
        return self.trend

    def choch_against(self, side: Side, since: int) -> bool:
        """A CHoCH against ``side`` happened at or after ``since``."""
        want = "choch_down" if side is Side.LONG else "choch_up"
        return any(t >= since and kind == want for t, kind in self.events)


@dataclass(frozen=True)
class Leg:
    up: bool
    slope: float  # normalised, signed


def legs(swings: list[Swing], tf_ms: int, atr_d1: float, close: float) -> list[Leg]:
    if not (math.isfinite(atr_d1) and atr_d1 > 0 and close > 0):
        return []
    bars_per_day = 86_400_000 / tf_ms
    vol = atr_d1 / close
    out = []
    for a, b in zip(swings, swings[1:], strict=False):
        bars = max((b.pivot_time - a.pivot_time) / tf_ms, 1.0)
        slope = (b.price - a.price) / a.price / bars * bars_per_day / vol
        out.append(Leg(b.price > a.price, slope))
    return out


def impulse_and_correction(all_legs: list[Leg], side: Side) -> tuple[list[float], list[float]]:
    """|slope| of impulse legs and of correction legs for ``side``, oldest first."""
    up = side is Side.LONG
    imp = [abs(leg.slope) for leg in all_legs if leg.up == up]
    cor = [abs(leg.slope) for leg in all_legs if leg.up != up]
    return imp, cor


def slope_ok(all_legs: list[Leg], side: Side) -> bool:
    """The latest impulse is steeper than the latest correction."""
    imp, cor = impulse_and_correction(all_legs, side)
    return bool(imp and cor and imp[-1] > cor[-1])


def momentum_decay(all_legs: list[Leg], side: Side, ratio: float) -> bool:
    imp, _ = impulse_and_correction(all_legs, side)
    if len(imp) < 4:
        return False
    return imp[-1] < ratio * statistics.median(imp[-4:-1])
