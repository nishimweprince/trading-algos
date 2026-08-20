"""Intrabar path resolver ladder.

Same-bar lock-then-target is the measurement risk this project has. The default
``m1_conservative`` mode re-checks the newly locked stop on that bar and does not
prefer the target when both are touched. ``tick`` is an interface only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from models import Candle, IntrabarMode


@dataclass(frozen=True, slots=True)
class LevelHit:
    kind: Literal["stop", "tp", "none"]
    fill: float | None


class TickPathUnavailable(NotImplementedError):
    """Raised when INTRABAR_MODE=tick is selected without a tick source."""


def _stop_hit(bar: Candle, stop: float, *, is_long: bool) -> bool:
    return bar.low <= stop if is_long else bar.high >= stop


def _tp_hit(bar: Candle, tp: float, *, is_long: bool) -> bool:
    return bar.high >= tp if is_long else bar.low <= tp


def _fill_stop(open_px: float, level: float, *, is_long: bool) -> float:
    if is_long:
        return open_px if open_px <= level else level
    return open_px if open_px >= level else level


def _fill_limit(open_px: float, level: float, *, is_long: bool) -> float:
    if is_long:
        return open_px if open_px >= level else level
    return open_px if open_px <= level else level


def m1_covering(parent: Candle, m1_bars: list[Candle], parent_minutes: int) -> list[Candle]:
    start = parent.ts - timedelta(minutes=parent_minutes)
    return [bar for bar in m1_bars if start < bar.ts <= parent.ts]


def walk_m1(
    bars: list[Candle],
    *,
    is_long: bool,
    stop: float,
    tp: float,
    conservative: bool,
) -> LevelHit:
    for bar in bars:
        hit_stop = _stop_hit(bar, stop, is_long=is_long)
        hit_tp = _tp_hit(bar, tp, is_long=is_long)
        if hit_stop and hit_tp:
            if conservative:
                return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
            return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
        if hit_stop:
            return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
        if hit_tp:
            return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
    return LevelHit("none", None)


def after_lock_same_bar(
    *,
    mode: IntrabarMode,
    is_long: bool,
    bar: Candle,
    stop: float,
    tp: float,
    m1_bars: list[Candle] | None,
    parent_minutes: int,
) -> LevelHit:
    """Decide the survivor's same-bar fate after the hedge is stopped and locked."""
    if mode is IntrabarMode.TICK:
        raise TickPathUnavailable("INTRABAR_MODE=tick requires a tick source (not implemented)")

    hit_stop = _stop_hit(bar, stop, is_long=is_long)
    hit_tp = _tp_hit(bar, tp, is_long=is_long)
    covering = m1_covering(bar, m1_bars or [], parent_minutes)

    if mode is IntrabarMode.M1 and covering:
        return walk_m1(covering, is_long=is_long, stop=stop, tp=tp, conservative=False)
    if mode is IntrabarMode.M1_CONSERVATIVE and covering:
        return walk_m1(covering, is_long=is_long, stop=stop, tp=tp, conservative=True)

    if mode is IntrabarMode.OPTIMISTIC:
        if hit_tp:
            return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
        return LevelHit("none", None)

    # pessimistic, and m1 / m1_conservative without M1 coverage
    if hit_stop and hit_tp:
        return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
    if hit_stop:
        return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
    if hit_tp:
        return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
    return LevelHit("none", None)
