"""Intrabar path resolver ladder.

Same-bar lock-then-target is the measurement risk this project has. The default
``m1_conservative`` mode re-checks the newly locked stop on that bar and does not
prefer the target when both are touched. ``tick`` is an interface only.

Frozen fill contract (Phase 5 review):

- Stop loss: never better than its level. An adverse opening gap fills at the bar open.
- Stop entry: never better than its trigger. An adverse opening gap fills at the bar open.
- Profit-taking limit: level-or-better is permitted on a favorable opening gap.
- Every fill must remain inside the bar's OHLC range and follow resolver chronology.
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


@dataclass(frozen=True, slots=True)
class OcoTriggerHit:
    side: Literal["long", "short", "none"]
    fill: float | None
    gap: bool = False
    ambiguous: bool = False
    child_index: int | None = None


class TickPathUnavailable(NotImplementedError):
    """Raised when INTRABAR_MODE=tick is selected without a tick source."""


def _stop_hit(bar: Candle, stop: float, *, is_long: bool) -> bool:
    return bar.low <= stop if is_long else bar.high >= stop


def _tp_hit(bar: Candle, tp: float, *, is_long: bool) -> bool:
    return bar.high >= tp if is_long else bar.low <= tp


def _fill_stop(open_px: float, level: float, *, is_long: bool) -> float:
    """Stop-loss fill: never better than ``level``; adverse gaps fill at the open."""
    if is_long:
        return open_px if open_px <= level else level
    return open_px if open_px >= level else level


def _fill_limit(open_px: float, level: float, *, is_long: bool) -> float:
    """Profit-taking limit: level-or-better is allowed on a favorable opening gap."""
    if is_long:
        return open_px if open_px >= level else level
    return open_px if open_px <= level else level


def m1_covering(parent: Candle, m1_bars: list[Candle], parent_minutes: int) -> list[Candle]:
    start = parent.ts - timedelta(minutes=parent_minutes)
    return [bar for bar in m1_bars if start < bar.ts <= parent.ts]


def covering_status(
    covering: list[Candle], parent_minutes: int
) -> Literal["complete", "partial", "absent"]:
    """Classify M1 coverage for one parent bar at a resolver call site."""
    if not covering:
        return "absent"
    if len(covering) >= parent_minutes:
        return "complete"
    return "partial"


def fill_inside_ohlc(bar: Candle, fill: float) -> bool:
    return bar.low <= fill <= bar.high


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


def resolve_bar_levels(
    *,
    mode: IntrabarMode,
    is_long: bool,
    bar: Candle,
    stop: float,
    tp: float,
    m1_bars: list[Candle] | None,
    parent_minutes: int,
) -> LevelHit:
    """Resolve the first ordinary stop/target hit within one completed bar."""
    if mode is IntrabarMode.TICK:
        raise TickPathUnavailable("INTRABAR_MODE=tick requires a tick source (not implemented)")
    hit_stop = _stop_hit(bar, stop, is_long=is_long)
    hit_tp = _tp_hit(bar, tp, is_long=is_long)
    covering = m1_covering(bar, m1_bars or [], parent_minutes)
    if mode is IntrabarMode.M1 and covering:
        return walk_m1(covering, is_long=is_long, stop=stop, tp=tp, conservative=False)
    if mode is IntrabarMode.M1_CONSERVATIVE and covering:
        return walk_m1(covering, is_long=is_long, stop=stop, tp=tp, conservative=True)
    if hit_stop and hit_tp:
        if mode is IntrabarMode.OPTIMISTIC:
            return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
        return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
    if hit_stop:
        return LevelHit("stop", _fill_stop(bar.open, stop, is_long=is_long))
    if hit_tp:
        return LevelHit("tp", _fill_limit(bar.open, tp, is_long=is_long))
    return LevelHit("none", None)


def _oco_in_one_bar(
    *,
    bar: Candle,
    upper: float,
    lower: float,
    preferred_long: bool,
    conservative: bool,
) -> OcoTriggerHit:
    if bar.open >= upper:
        return OcoTriggerHit("long", bar.open, gap=bar.open > upper)
    if bar.open <= lower:
        return OcoTriggerHit("short", bar.open, gap=bar.open < lower)
    long_hit = bar.high >= upper
    short_hit = bar.low <= lower
    if long_hit and short_hit:
        choose_long = preferred_long if not conservative else not preferred_long
        return OcoTriggerHit(
            "long" if choose_long else "short",
            upper if choose_long else lower,
            ambiguous=True,
        )
    if long_hit:
        return OcoTriggerHit("long", upper)
    if short_hit:
        return OcoTriggerHit("short", lower)
    return OcoTriggerHit("none", None)


def resolve_oco_trigger(
    *,
    mode: IntrabarMode,
    bullish_signal: bool,
    bar: Candle,
    upper: float,
    lower: float,
    m1_bars: list[Candle] | None,
    parent_minutes: int,
) -> OcoTriggerHit:
    """Resolve a two-sided stop-entry OCO with the same path tiers as exits."""
    if mode is IntrabarMode.TICK:
        raise TickPathUnavailable("INTRABAR_MODE=tick requires a tick source (not implemented)")
    covering = m1_covering(bar, m1_bars or [], parent_minutes)
    if mode in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE} and covering:
        conservative = mode is IntrabarMode.M1_CONSERVATIVE
        for index, child in enumerate(covering):
            hit = _oco_in_one_bar(
                bar=child,
                upper=upper,
                lower=lower,
                preferred_long=bullish_signal,
                conservative=conservative,
            )
            if hit.side != "none":
                return OcoTriggerHit(
                    side=hit.side,
                    fill=hit.fill,
                    gap=hit.gap,
                    ambiguous=hit.ambiguous,
                    child_index=index,
                )
        return OcoTriggerHit("none", None)
    return _oco_in_one_bar(
        bar=bar,
        upper=upper,
        lower=lower,
        preferred_long=bullish_signal,
        conservative=mode is not IntrabarMode.OPTIMISTIC,
    )


@dataclass(frozen=True, slots=True)
class RatchetHit:
    """Level resolution for a leg carrying an MFE-armed breakeven ratchet.

    ``stop`` and ``armed`` are the state carried into the next bar. The ratchet is
    only ever armed for *subsequent* path segments, never for the segment that
    armed it, so no bar's own extreme can move a stop that the same bar then fills.
    """

    kind: Literal["stop", "tp", "none"]
    fill: float | None
    stop: float
    armed: bool


def _favourable_extreme(bar: Candle, *, is_long: bool) -> float:
    return bar.high if is_long else bar.low


def _reached(level: float, extreme: float, *, is_long: bool) -> bool:
    return extreme >= level if is_long else extreme <= level


def resolve_bar_levels_ratchet(
    *,
    mode: IntrabarMode,
    is_long: bool,
    bar: Candle,
    stop: float,
    tp: float,
    arm_level: float,
    ratchet_stop: float,
    armed: bool,
    m1_bars: list[Candle] | None,
    parent_minutes: int,
) -> RatchetHit:
    """Resolve one bar for a leg whose stop ratchets to ``ratchet_stop`` past ``arm_level``.

    With M1 coverage the arming point is located chronologically inside the bar, so a
    trade that runs to ``arm_level`` and then reverses is stopped at ``ratchet_stop``
    rather than at its original stop. Without coverage the ratchet arms at the bar
    boundary, which is the conservative reading: the original stop stays in force for
    the whole bar that first touches ``arm_level``.
    """
    if mode is IntrabarMode.TICK:
        raise TickPathUnavailable("INTRABAR_MODE=tick requires a tick source (not implemented)")

    covering = m1_covering(bar, m1_bars or [], parent_minutes)
    conservative = mode is not IntrabarMode.OPTIMISTIC

    if mode in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE} and covering:
        current = stop
        is_armed = armed
        for child in covering:
            hit_stop = _stop_hit(child, current, is_long=is_long)
            hit_tp = _tp_hit(child, tp, is_long=is_long)
            if hit_stop and hit_tp:
                if mode is IntrabarMode.M1_CONSERVATIVE:
                    return RatchetHit(
                        "stop", _fill_stop(child.open, current, is_long=is_long), current, is_armed
                    )
                return RatchetHit(
                    "tp", _fill_limit(child.open, tp, is_long=is_long), current, is_armed
                )
            if hit_stop:
                return RatchetHit(
                    "stop", _fill_stop(child.open, current, is_long=is_long), current, is_armed
                )
            if hit_tp:
                return RatchetHit(
                    "tp", _fill_limit(child.open, tp, is_long=is_long), current, is_armed
                )
            if not is_armed and _reached(
                arm_level, _favourable_extreme(child, is_long=is_long), is_long=is_long
            ):
                is_armed = True
                current = ratchet_stop
        return RatchetHit("none", None, current, is_armed)

    hit_stop = _stop_hit(bar, stop, is_long=is_long)
    hit_tp = _tp_hit(bar, tp, is_long=is_long)
    if hit_stop and hit_tp:
        if conservative:
            return RatchetHit("stop", _fill_stop(bar.open, stop, is_long=is_long), stop, armed)
        return RatchetHit("tp", _fill_limit(bar.open, tp, is_long=is_long), stop, armed)
    if hit_stop:
        return RatchetHit("stop", _fill_stop(bar.open, stop, is_long=is_long), stop, armed)
    if hit_tp:
        return RatchetHit("tp", _fill_limit(bar.open, tp, is_long=is_long), stop, armed)

    if not armed and _reached(
        arm_level, _favourable_extreme(bar, is_long=is_long), is_long=is_long
    ):
        return RatchetHit("none", None, ratchet_stop, True)
    return RatchetHit("none", None, stop, armed)
