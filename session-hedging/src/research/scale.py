"""S8 scale decomposition: the complete 256-cell §8.1 grid on one candle set.

Every cell shares one candle fingerprint, one date range, and one configuration;
only ``ENTRY_MODE``, ``ORB_MINUTES``, ``ENTRY_DELAY_MINUTES`` and ``MAX_AGE_HOURS``
vary. This is descriptive measurement, not selection: the harness reports the whole
surface, losing cells included, and never chooses a production configuration.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import product
from typing import Literal

from anchors import SessionAnchor
from cell_stats import CompletedStructure, candle_sha256, completed_structures, shared_cell_metrics
from engine import ClosedBarEngine
from models import (
    Candle,
    EngineParams,
    EntryMode,
    HoldBucketAttribution,
    IntrabarMode,
    M1CoverageReport,
    ScaleSweepCell,
    ScaleSweepReport,
    TimeExitMode,
    Timeframe,
)
from sessions import SessionWindow

S8_ENTRY_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
S8_ORB_MINUTES: tuple[int, ...] = (15, 30, 60, 120)
S8_ENTRY_DELAY_MINUTES: tuple[int, ...] = (0, 15, 30, 60)
S8_MAX_AGE_HOURS: tuple[float, ...] = (8.0, 12.0, 24.0, 48.0)
S8_CELL_COUNT = (
    len(S8_ENTRY_MODES)
    * len(S8_ORB_MINUTES)
    * len(S8_ENTRY_DELAY_MINUTES)
    * len(S8_MAX_AGE_HOURS)
)
S8_VARIED_FIELDS = frozenset(
    {"entry_mode", "orb_minutes", "entry_delay_minutes", "max_age_hours"}
)


@dataclass(frozen=True, slots=True)
class ScaleCoordinate:
    entry_mode: EntryMode
    orb_minutes: int
    entry_delay_minutes: int
    max_age_hours: float


@dataclass(frozen=True, slots=True)
class HoldBucket:
    label: str
    lower_hours: float
    upper_hours: float | None
    lower_inclusive: bool
    upper_inclusive: bool

    def contains(self, hours: float) -> bool:
        above = hours >= self.lower_hours if self.lower_inclusive else hours > self.lower_hours
        if not above:
            return False
        if self.upper_hours is None:
            return True
        return hours <= self.upper_hours if self.upper_inclusive else hours < self.upper_hours


HOLD_BUCKETS: tuple[HoldBucket, ...] = (
    HoldBucket("[0h,8h]", 0.0, 8.0, True, True),
    HoldBucket("(8h,12h]", 8.0, 12.0, False, True),
    HoldBucket("(12h,24h]", 12.0, 24.0, False, True),
    HoldBucket("(24h,48h]", 24.0, 48.0, False, True),
    HoldBucket("(48h,+inf)", 48.0, None, False, False),
)

_NO_SUBPATH_FALLBACK = (
    "pessimistic_same_bar_no_subpath",
    "No covering M1 bars were available, so the resolver used its documented "
    "conservative same-bar fallback: when a bar touches both the stop and the target "
    "the stop is taken first, and no M1 chronology was consulted.",
)
_FALLBACKS: dict[IntrabarMode, tuple[str, str]] = {
    IntrabarMode.M1: _NO_SUBPATH_FALLBACK,
    IntrabarMode.M1_CONSERVATIVE: _NO_SUBPATH_FALLBACK,
    IntrabarMode.PESSIMISTIC: (
        "pessimistic_same_bar_stop_first",
        "INTRABAR_MODE=pessimistic never consults M1 subpaths: an ambiguous bar is "
        "resolved to the stop.",
    ),
    IntrabarMode.OPTIMISTIC: (
        "optimistic_same_bar_target_first",
        "INTRABAR_MODE=optimistic never consults M1 subpaths: an ambiguous bar is "
        "resolved to the target.",
    ),
}


def s8_grid() -> list[ScaleCoordinate]:
    """The full Cartesian product, in a fixed, reproducible order."""
    return [
        ScaleCoordinate(mode, orb, delay, max_age)
        for mode, orb, delay, max_age in product(
            S8_ENTRY_MODES, S8_ORB_MINUTES, S8_ENTRY_DELAY_MINUTES, S8_MAX_AGE_HOURS
        )
    ]


def base_params(params: EngineParams) -> EngineParams:
    """The shared configuration every cell inherits, with the S8 time exit forced on."""
    return EngineParams.model_validate(
        params.model_dump() | {"time_exit_mode": TimeExitMode.MAX_AGE}
    )


def cell_params(base: EngineParams, coordinate: ScaleCoordinate) -> EngineParams:
    """Validate, never unchecked-copy, the parameters for one cell."""
    return EngineParams.model_validate(
        base.model_dump()
        | {
            "entry_mode": coordinate.entry_mode,
            "orb_minutes": coordinate.orb_minutes,
            "entry_delay_minutes": coordinate.entry_delay_minutes,
            "max_age_hours": coordinate.max_age_hours,
        }
    )


def run_scale_sweep(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> ScaleSweepReport:
    """Run all 256 cells over one immutable candle set without mutating any input."""
    if not candles:
        raise ValueError("S8 requires at least one candle")

    base = base_params(params)
    coverage = m1_coverage(candles, m1_bars or [], base)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []

    cells: list[ScaleSweepCell] = []
    for index, coordinate in enumerate(s8_grid()):
        cell = cell_params(base, coordinate)
        _assert_only_varied_fields(base, cell)
        engine = ClosedBarEngine(windows, cell, anchors, subpath_bars)
        engine.run(candles)
        report = engine.report(symbol, timeframe, source).model_copy(
            update={"bar_count": len(candles)}
        )
        completed = completed_structures(engine, report)
        metrics = shared_cell_metrics(engine, report, completed)
        buckets, unbucketed = hold_bucket_attribution(completed)
        cells.append(
            ScaleSweepCell(
                cell_index=index,
                entry_mode=coordinate.entry_mode,
                orb_minutes=coordinate.orb_minutes,
                entry_delay_minutes=coordinate.entry_delay_minutes,
                max_age_hours=coordinate.max_age_hours,
                time_exit_mode=cell.time_exit_mode,
                completed_gross_pips=sum(item.gross_pips for item in completed),
                completed_net_pips=sum(item.net_pips for item in completed),
                completed_gross_r=sum(item.gross_r for item in completed),
                completed_net_r=sum(item.net_r for item in completed),
                tp_rate_margin_pp=report.tp_rate_margin_pp,
                tp_rate_margin_pp_ci_low=report.tp_rate_margin_pp_ci_low,
                tp_rate_margin_pp_ci_high=report.tp_rate_margin_pp_ci_high,
                hold_buckets=buckets,
                unbucketed_structures=unbucketed,
                **metrics,
            )
        )

    shared_params = base.model_dump(mode="json")
    for field in S8_VARIED_FIELDS:
        shared_params.pop(field, None)

    return ScaleSweepReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        shared_params=shared_params,
        sessions=[window.name for window in windows],
        entry_modes=list(S8_ENTRY_MODES),
        orb_minutes_grid=list(S8_ORB_MINUTES),
        entry_delay_minutes_grid=list(S8_ENTRY_DELAY_MINUTES),
        max_age_hours_grid=list(S8_MAX_AGE_HOURS),
        expected_cell_count=S8_CELL_COUNT,
        hold_bucket_labels=[bucket.label for bucket in HOLD_BUCKETS],
        m1_coverage=coverage,
        cells=cells,
    )


def hold_bucket_attribution(
    completed: list[CompletedStructure],
) -> tuple[list[HoldBucketAttribution], int]:
    """Attribute gross and net pips/R to non-overlapping, exhaustive hold buckets."""
    totals = {
        bucket.label: {"n": 0, "gross_pips": 0.0, "net_pips": 0.0, "gross_r": 0.0, "net_r": 0.0}
        for bucket in HOLD_BUCKETS
    }
    unbucketed = 0
    for structure in completed:
        bucket = _bucket_for(structure.hold_hours)
        if bucket is None:
            unbucketed += 1
            continue
        entry = totals[bucket.label]
        entry["n"] += 1
        entry["gross_pips"] += structure.gross_pips
        entry["net_pips"] += structure.net_pips
        entry["gross_r"] += structure.gross_r
        entry["net_r"] += structure.net_r
    attribution = [
        HoldBucketAttribution(
            label=bucket.label,
            lower_hours=bucket.lower_hours,
            upper_hours=bucket.upper_hours,
            lower_inclusive=bucket.lower_inclusive,
            upper_inclusive=bucket.upper_inclusive,
            structures=int(totals[bucket.label]["n"]),
            gross_pips=totals[bucket.label]["gross_pips"],
            net_pips=totals[bucket.label]["net_pips"],
            gross_r=totals[bucket.label]["gross_r"],
            net_r=totals[bucket.label]["net_r"],
        )
        for bucket in HOLD_BUCKETS
    ]
    return attribution, unbucketed


def m1_coverage(
    candles: list[Candle], m1_bars: list[Candle], params: EngineParams
) -> M1CoverageReport:
    """State plainly whether M1 chronology was available for these parent bars."""
    stamps = sorted(bar.ts for bar in m1_bars)
    span = timedelta(minutes=params.timeframe_minutes)
    # m1_covering() treats the parent window as half-open, ``(ts - span, ts]``.
    covered = sum(
        1 for candle in candles if _has_covering_bar(stamps, candle.ts - span, candle.ts)
    )
    total = len(candles)
    fraction = covered / total if total else 0.0
    if covered == 0:
        status: Literal["absent", "partial", "complete"] = "absent"
    elif covered == total:
        status = "complete"
    else:
        status = "partial"
    subpath_capable = params.intrabar_mode in {IntrabarMode.M1, IntrabarMode.M1_CONSERVATIVE}
    # One window, one resolver tier. Partial M1 coverage would resolve part of the window
    # on M1 chronology and the rest on the fallback, which makes cells inside one study
    # incomparable; the uniform fallback is used instead, and said so in the output.
    subpath_used = subpath_capable and status == "complete"
    if subpath_used:
        fallback: str | None = None
        description = (
            "Covering M1 bars were present for every parent bar, so the resolver used "
            "M1 subpath chronology and no fallback applied."
        )
    elif subpath_capable and status == "partial":
        fallback = _NO_SUBPATH_FALLBACK[0]
        description = (
            f"M1 bars were present but covered only {covered} of {total} parent bars "
            f"({covered / total:.2%} of the window). Mixing M1 chronology on part of the "
            "window with the fallback on the rest would make results inside one study "
            "incomparable, so no M1 chronology was used: the whole window was resolved with "
            f"the conservative {_NO_SUBPATH_FALLBACK[0]} fallback, in which a bar touching "
            "both the stop and the target is taken as the stop."
        )
    else:
        fallback, description = _FALLBACKS[params.intrabar_mode]
    return M1CoverageReport(
        intrabar_mode=params.intrabar_mode,
        status=status,
        m1_bars_loaded=len(m1_bars),
        m1_first_bar_ts=stamps[0] if stamps else None,
        m1_last_bar_ts=stamps[-1] if stamps else None,
        covered_parent_bars=covered,
        total_parent_bars=total,
        covered_parent_fraction=fraction,
        subpath_used=subpath_used,
        subpath_fallback=fallback,
        fallback_description=description,
    )


def _has_covering_bar(stamps: list[datetime], start: datetime, end: datetime) -> bool:
    index = bisect_right(stamps, start)
    return index < len(stamps) and stamps[index] <= end


def _bucket_for(hours: float | None) -> HoldBucket | None:
    if hours is None:
        return None
    for bucket in HOLD_BUCKETS:
        if bucket.contains(hours):
            return bucket
    return None


def _assert_only_varied_fields(base: EngineParams, cell: EngineParams) -> None:
    base_dump = base.model_dump()
    cell_dump = cell.model_dump()
    differing = {
        field for field in base_dump if base_dump[field] != cell_dump[field]
    }
    unexpected = differing - S8_VARIED_FIELDS
    if unexpected:
        raise ValueError(
            "S8 cells may only vary the four grid fields; changed: "
            + ", ".join(sorted(unexpected))
        )
