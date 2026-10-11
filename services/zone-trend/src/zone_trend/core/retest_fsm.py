"""Per-zone break-and-retest state machine (entry type B), stepped on 1H closes.

Long (shorts mirror throughout):

IDLE -> BROKEN
    The zone is resistance; the previous close was at or below ``hi + buf`` and
    this bar closes above it, where ``buf = 0.10 * ATR14(entry)``. The bar is
    bullish with body >= 50% of its range and volume >= 1.3 x the median of
    the 20 bars before it. The 4H trend is Up, or Range with zone strength
    >= 70. A confirmed 1H swing low below the zone exists (it is A).
BROKEN -> RETESTING
    A later bar trades back to within ``hi + 0.25 * ATR``.
RETESTING -> ENTERED (emits an ``Armed``)
    A bar closes back above ``hi`` with a rejection candle (lower wick >= 40%
    of the range, close in the top third) while the retracement of A -> B is in
    the band. B is the highest high since the breakout and P the lowest low
    since it.
-> FAILED
    A close below ``lo - 0.25 * ATR`` (back through the zone), a retracement
    over 1.0 (CHoCH), or ``retest_window_bars`` without an entry.
ENTERED / FAILED -> IDLE
    After the zone's next role flip, or ``reset_bars``. So there is one signal
    per zone per break.

The zone's edges are frozen at the breakout, so a zone rebuild during a
retest does not move the goalposts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from zone_trend.config import Entries
from zone_trend.core.fib import in_band, retracement
from zone_trend.types import Bar, Side, Trend, Zone

__all__ = ["Armed", "FsmCtx", "RetestFSM"]

IDLE, BROKEN, RETESTING, ENTERED, FAILED = "IDLE", "BROKEN", "RETESTING", "ENTERED", "FAILED"
RESET_BARS = 24
RANGE_MIN_STRENGTH_DEFAULT = 70.0


@dataclass(frozen=True)
class FsmCtx:
    atr: float  # ATR14(entry) at this bar
    vol_med: float  # median volume of the previous 20 entry bars
    prev_close: float
    trend: Trend  # fast-TF trend state
    swing_low: float | None  # last confirmed entry-TF swing low price
    swing_high: float | None
    range_min_strength: float = RANGE_MIN_STRENGTH_DEFAULT


@dataclass(frozen=True)
class Armed:
    zone_id: str
    side: Side
    lo: float
    hi: float
    a: float
    b: float
    p: float  # retest extreme
    retr: float
    breakout_time: int
    bar_time: int  # close time of the arming bar
    close: float


def rejection(bar: Bar, side: Side, min_wick: float, close_frac: float) -> bool:
    rng = bar.high - bar.low
    if rng <= 0:
        return False
    if side is Side.LONG:
        wick = min(bar.open, bar.close) - bar.low
        return wick >= min_wick * rng and bar.close - bar.low >= (1 - close_frac) * rng
    wick = bar.high - max(bar.open, bar.close)
    return wick >= min_wick * rng and bar.high - bar.close >= (1 - close_frac) * rng


class RetestFSM:
    __slots__ = (
        "zone_id",
        "reset_bars",
        "state",
        "side",
        "lo",
        "hi",
        "a",
        "b",
        "p",
        "breakout_time",
        "bars_since",
        "idle_bars",
        "flips_at_end",
        "strength",
    )

    def __init__(self, zone_id: str, reset_bars: int = RESET_BARS):
        self.zone_id = zone_id
        self.reset_bars = reset_bars
        self.state = IDLE
        self.side: Side | None = None
        self.lo = self.hi = self.a = self.b = math.nan
        self.p = math.nan
        self.breakout_time = 0
        self.bars_since = 0
        self.idle_bars = 0
        self.flips_at_end = 0
        self.strength = 0.0

    @property
    def active(self) -> bool:
        return self.state in (BROKEN, RETESTING)

    def step(
        self, bar: Bar, zone: Zone | None, ctx: FsmCtx, cfg: Entries, *, bar_scale: int = 1
    ) -> Armed | None:
        """Step one entry bar; ``bar_scale`` is entry bars per hour, so the
        retest window keeps its wall-clock span whatever the entry TF."""
        if self.state == IDLE:
            if zone is not None:
                self._try_break(bar, zone, ctx, cfg)
            return None
        if self.state in (ENTERED, FAILED):
            self.idle_bars += 1
            flipped = zone is not None and zone.flips > self.flips_at_end
            if flipped or self.idle_bars >= self.reset_bars:
                self.state = IDLE
                self.side = None
            return None
        return self._track(bar, zone, ctx, cfg, bar_scale)

    # -- IDLE ------------------------------------------------------------------
    def _try_break(self, bar: Bar, z: Zone, ctx: FsmCtx, cfg: Entries) -> None:
        if not (math.isfinite(ctx.atr) and math.isfinite(ctx.vol_med) and ctx.atr > 0):
            return
        buf = cfg.breakout_buffer_atr * ctx.atr
        rng = bar.high - bar.low
        if rng <= 0:
            return
        strong = abs(bar.close - bar.open) >= cfg.min_body_ratio * rng
        loud = bar.volume >= cfg.volume_mult * ctx.vol_med
        if not (strong and loud):
            return
        ranging = ctx.trend == Trend.RANGE and z.strength >= ctx.range_min_strength
        if (
            z.role == "resistance"
            and ctx.prev_close <= z.hi + buf < bar.close
            and bar.close > bar.open
            and (ctx.trend == Trend.UP or ranging)
            and ctx.swing_low is not None
            and ctx.swing_low < z.hi
        ):
            self._start(Side.LONG, z, ctx.swing_low, bar.high, bar.close_time)
        elif (
            z.role == "support"
            and ctx.prev_close >= z.lo - buf > bar.close
            and bar.close < bar.open
            and (ctx.trend == Trend.DOWN or ranging)
            and ctx.swing_high is not None
            and ctx.swing_high > z.lo
        ):
            self._start(Side.SHORT, z, ctx.swing_high, bar.low, bar.close_time)

    def _start(self, side: Side, z: Zone, a: float, b: float, t: int) -> None:
        self.state = BROKEN
        self.side = side
        self.lo, self.hi = z.lo, z.hi
        self.a, self.b = a, b
        self.p = math.nan
        self.breakout_time = t
        self.bars_since = 0
        self.strength = z.strength

    # -- BROKEN / RETESTING -------------------------------------------------------
    def _end(self, state: str, zone: Zone | None) -> None:
        self.state = state
        self.idle_bars = 0
        self.flips_at_end = zone.flips if zone is not None else 0

    def _track(
        self, bar: Bar, zone: Zone | None, ctx: FsmCtx, cfg: Entries, bar_scale: int
    ) -> Armed | None:
        self.bars_since += 1
        if zone is not None:
            self.strength = zone.strength
        tol = cfg.retest_tolerance_atr * ctx.atr
        long = self.side is Side.LONG
        if long:
            self.b = max(self.b, bar.high)
            failed = bar.close < self.lo - tol
            touched = bar.low <= self.hi + tol
            extreme = bar.low
        else:
            self.b = min(self.b, bar.low)
            failed = bar.close > self.hi + tol
            touched = bar.high >= self.lo - tol
            extreme = bar.high
        if failed or self.bars_since > cfg.retest_window_bars * bar_scale:
            self._end(FAILED, zone)
            return None
        if self.state == BROKEN and touched:
            self.state = RETESTING
        if self.state != RETESTING:
            return None
        if math.isnan(self.p):
            self.p = extreme
        else:
            self.p = min(self.p, extreme) if long else max(self.p, extreme)
        retr = retracement(self.a, self.b, self.p)
        if retr > 1.0:
            self._end(FAILED, zone)
            return None
        outside = bar.close > self.hi if long else bar.close < self.lo
        if (
            outside
            and rejection(bar, self.side, cfg.rejection_min_wick, cfg.rejection_close_frac)
            and in_band(retr, cfg.fib_band)
        ):
            self._end(ENTERED, zone)
            return Armed(
                self.zone_id,
                self.side,
                self.lo,
                self.hi,
                self.a,
                self.b,
                self.p,
                retr,
                self.breakout_time,
                bar.close_time,
                bar.close,
            )
        return None
