"""Position management on each 1H close.

Fills inside the hour (stop, TP1, TP2) are the broker's (simulated or real);
this module decides what to change at the close:

- CHoCH on 4H against the position: exit at the next open.
- Time stop: after ``time_stop_bars`` bars with the favourable excursion still
  under ``time_stop_min_r`` R, exit at the next open.
- After TP1: the stop goes to entry plus round-trip fees at once, then trails
  at the best price since entry minus ``trail_atr`` x ATR14(1H)
  (``trail_atr_decay`` on momentum decay). The trail only ever tightens and
  only moves when the step is at least ``trail_min_step_atr`` x ATR14(1H).
"""

from __future__ import annotations

from dataclasses import dataclass

from zone_trend.config import Exits
from zone_trend.types import Bar, Position, Side

__all__ = ["ExitNextOpen", "MoveStop", "evaluate", "observe"]


@dataclass(frozen=True)
class MoveStop:
    price: float
    reason: str  # "breakeven" | "trail"


@dataclass(frozen=True)
class ExitNextOpen:
    reason: str  # "choch" | "time"


def observe(pos: Position, bar: Bar) -> None:
    """Count the bar and update the excursions (call once per 1H close)."""
    pos.bars_open += 1
    if pos.side is Side.LONG:
        pos.best = max(pos.best, bar.high)
        pos.worst = min(pos.worst, bar.low)
    else:
        pos.best = min(pos.best, bar.low)
        pos.worst = max(pos.worst, bar.high)


def evaluate(
    pos: Position,
    atr_h1: float,
    *,
    decay: bool,
    choch_against: bool,
    fee_taker: float,
    cfg: Exits,
) -> list[MoveStop | ExitNextOpen]:
    if choch_against:
        return [ExitNextOpen("choch")]
    if pos.bars_open >= cfg.time_stop_bars and pos.mfe_r < cfg.time_stop_min_r:
        return [ExitNextOpen("time")]
    if not pos.tp1_done:
        return []

    sign = pos.side.sign
    breakeven = pos.entry * (1 + sign * 2 * fee_taker)
    if sign * (pos.stop - breakeven) < 0:
        return [MoveStop(breakeven, "breakeven")]
    mult = cfg.trail_atr_decay if decay else cfg.trail_atr
    trail = pos.best - sign * mult * atr_h1
    if sign * (trail - pos.stop) >= cfg.trail_min_step_atr * atr_h1:
        return [MoveStop(trail, "trail")]
    return []
