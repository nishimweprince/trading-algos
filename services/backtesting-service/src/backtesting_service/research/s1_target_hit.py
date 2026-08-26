"""S1: conditional target-hit study.

The pilot's reach frequencies are any-direction upper bounds. The strategy needs the
conditional version: given that the first stop actually occurred, how far does the
*survivor* travel, in R, within a bounded horizon, and does it get there before the
lock takes it out. This is the study that selects ``RR``, so it must measure reach
beyond the configured target — a run censored at ``3R`` can never justify ``4R``.

With no covering M1 data the forward walk uses completed parent bars. Two consequences
are stated rather than smoothed over: a bar's high and low are credited to the whole
bar, which makes every reach frequency an **upper bound**; and when one bar touches
both the lock and a reach level, the lock is taken first.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean, median
from typing import Literal

from ..anchors import SessionAnchor, session_anchor_ts
from ..cell_stats import candle_sha256, pair_outcome
from ..engine import ClosedBarEngine, Pair
from ..metrics import OutcomeKind, wilson_interval
from ..models import (
    BacktestReport,
    Candle,
    EngineParams,
    EntryMode,
    ReachRate,
    S1ConditioningSummary,
    S1ExcursionCell,
    S1ReachCell,
    S1Structure,
    S1TargetHitReport,
    Timeframe,
    TradePairLeg,
    TradePairResult,
)
from ..sessions import SessionWindow
from . import markdown
from .episodes import (
    ATR_PERIOD,
    Episode,
    build_episodes,
    tercile_edges,
    tercile_label,
)
from .scale import m1_coverage

S1_K_VALUES: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
S1_HORIZON_HOURS: tuple[float, ...] = (1.0, 4.0, 8.0, 12.0, 24.0, 48.0)
S1_REFERENCE_MODE = EntryMode.HEDGE_PAIR


@dataclass(frozen=True, slots=True)
class _Conditioned:
    pair: Pair
    result: TradePairResult
    survivor: TradePairLeg
    first_stop_ts: datetime
    survivor_entry: float
    outcome: OutcomeKind


def run_s1_target_hit(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> S1TargetHitReport:
    """Condition on the first stop, then walk the survivor forward over fixed horizons."""
    if not candles:
        raise ValueError("S1 requires at least one candle")

    reference = EngineParams.model_validate(params.model_dump() | {"entry_mode": S1_REFERENCE_MODE})
    coverage = m1_coverage(candles, m1_bars or [], reference)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []
    engine = ClosedBarEngine(windows, reference, anchors, subpath_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(update={"bar_count": len(candles)})

    episodes = build_episodes(candles, anchors, reference)
    conditioned, summary_counts = _condition(engine, report)
    lock_dist = reference.lock_pips * reference.pip_size

    atr_by_structure: dict[str, float | None] = {}
    orb_by_structure: dict[str, float | None] = {}
    for item in conditioned:
        episode = _episode_for(item, episodes, anchors, reference)
        atr_by_structure[item.pair.id] = episode.atr_pips if episode else None
        orb_by_structure[item.pair.id] = episode.orb_range_pips if episode else None

    edges = tercile_edges([value for value in atr_by_structure.values() if value is not None])

    structures: list[S1Structure] = []
    walks: list[tuple[S1Structure, str, float | None, float | None]] = []
    no_forward = 0
    lock_touched = 0
    lock_collapsed = 0
    for item in conditioned:
        is_long = item.survivor.side == "long"
        walk = survivor_excursions(
            candles,
            first_stop_ts=item.first_stop_ts,
            entry=item.survivor_entry,
            sl_dist=item.pair.sl_dist,
            lock_price=lock_price_for(
                item.pair.entry, item.pair.sl_dist, lock_dist, is_long=is_long
            ),
            is_long=is_long,
        )
        if walk is None:
            no_forward += 1
            continue
        if walk.lock_price == item.pair.entry:
            lock_collapsed += 1
        if walk.lock_touched_ts is not None:
            lock_touched += 1
        atr = atr_by_structure.get(item.pair.id)
        structure = S1Structure(
            pair_id=item.pair.id,
            session=item.pair.session,
            survivor_side=item.survivor.side,
            entry_ts=item.result.entry_ts,
            first_stop_ts=item.first_stop_ts,
            survivor_entry=item.survivor_entry,
            s_pips=item.pair.sl_dist / reference.pip_size,
            orb_range_pips=orb_by_structure.get(item.pair.id),
            atr_pips=atr,
            atr_tercile=tercile_label(atr, edges),
            lock_price=walk.lock_price,
            lock_touched_ts=walk.lock_touched_ts,
            forward_bars=walk.forward_bars,
            mfe_r_by_horizon=walk.mfe_r,
            mae_r_by_horizon=walk.mae_r,
            mfe_r_before_lock_by_horizon=walk.mfe_r_before_lock,
            realized_outcome=item.outcome,
        )
        structures.append(structure)
        walks.append((structure, structure.atr_tercile, atr, orb_by_structure.get(item.pair.id)))

    summary = S1ConditioningSummary(
        structures_total=len(report.trade_pairs),
        conditioned=len(structures),
        excluded_no_stop=summary_counts["no_stop"],
        excluded_simultaneous_stop=summary_counts["simultaneous"],
        excluded_not_two_legs=summary_counts["not_two_legs"],
        excluded_missing_entry=summary_counts["missing_entry"],
        excluded_no_forward_bars=no_forward,
        lock_touched=lock_touched,
        lock_distance_pips=reference.lock_pips,
        lock_collapsed_to_entry=lock_collapsed,
    )

    shared = reference.model_dump(mode="json")
    return S1TargetHitReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        reference_entry_mode=S1_REFERENCE_MODE,
        shared_params=shared,
        sessions=[window.name for window in windows],
        k_values=list(S1_K_VALUES),
        horizon_hours=list(S1_HORIZON_HOURS),
        atr_period=ATR_PERIOD,
        atr_tercile_edges_pips=list(edges) if edges else [],
        m1_coverage=coverage,
        conditioning=summary,
        reach_cells=_reach_cells(structures),
        excursions=_excursions(structures),
        structures=structures,
    )


@dataclass(frozen=True, slots=True)
class _Walk:
    lock_price: float
    lock_touched_ts: datetime | None
    forward_bars: int
    mfe_r: dict[str, float]
    mae_r: dict[str, float]
    mfe_r_before_lock: dict[str, float]


def lock_price_for(pair_entry: float, sl_dist: float, lock_dist: float, *, is_long: bool) -> float:
    """The engine's lock rule: ``entry ± LOCK_PIPS``, collapsing to entry when S is smaller."""
    if sl_dist >= lock_dist and lock_dist > 0:
        return pair_entry + lock_dist if is_long else pair_entry - lock_dist
    return pair_entry


def survivor_excursions(
    candles: list[Candle],
    *,
    first_stop_ts: datetime,
    entry: float,
    sl_dist: float,
    lock_price: float,
    is_long: bool,
) -> _Walk | None:
    """Walk completed bars after the stop bar. The stop bar itself is never credited."""
    if sl_dist <= 0:
        return None
    stamps = [candle.ts for candle in candles]
    start = bisect_right(stamps, first_stop_ts)
    if start >= len(candles):
        return None
    horizon_end = {
        horizon: first_stop_ts + timedelta(hours=horizon) for horizon in S1_HORIZON_HOURS
    }
    mfe = {_key(horizon): 0.0 for horizon in S1_HORIZON_HOURS}
    mae = {_key(horizon): 0.0 for horizon in S1_HORIZON_HOURS}
    before_lock = {_key(horizon): 0.0 for horizon in S1_HORIZON_HOURS}
    lock_touched_ts: datetime | None = None
    bars = 0
    longest = max(S1_HORIZON_HOURS)
    for candle in candles[start:]:
        if candle.ts > first_stop_ts + timedelta(hours=longest):
            break
        bars += 1
        favorable = (candle.high - entry) / sl_dist if is_long else (entry - candle.low) / sl_dist
        adverse = (entry - candle.low) / sl_dist if is_long else (candle.high - entry) / sl_dist
        touches_lock = candle.low <= lock_price if is_long else candle.high >= lock_price
        for horizon in S1_HORIZON_HOURS:
            if candle.ts > horizon_end[horizon]:
                continue
            key = _key(horizon)
            mfe[key] = max(mfe[key], favorable)
            mae[key] = max(mae[key], adverse)
            # No M1 subpath: a bar that touches the lock is taken as lock-first, so its
            # favourable extreme is not credited to the lock-survived series.
            if lock_touched_ts is None and not touches_lock:
                before_lock[key] = max(before_lock[key], favorable)
        if lock_touched_ts is None and touches_lock:
            lock_touched_ts = candle.ts
    return _Walk(
        lock_price=lock_price,
        lock_touched_ts=lock_touched_ts,
        forward_bars=bars,
        mfe_r=mfe,
        mae_r=mae,
        mfe_r_before_lock=before_lock,
    )


def _condition(
    engine: ClosedBarEngine, report: BacktestReport
) -> tuple[list[_Conditioned], dict[str, int]]:
    pairs = {pair.id: pair for pair in engine.pairs}
    counts = {"no_stop": 0, "simultaneous": 0, "not_two_legs": 0, "missing_entry": 0}
    conditioned: list[_Conditioned] = []
    for result in report.trade_pairs:
        legs = [
            leg for leg in (result.primary, result.hedge, *result.unknown_legs) if leg is not None
        ]
        if len(legs) != 2:
            counts["not_two_legs"] += 1
            continue
        stops = [
            leg
            for leg in legs
            if leg.status == "closed" and leg.bucket == "loss" and leg.exit_ts is not None
        ]
        if not stops:
            counts["no_stop"] += 1
            continue
        stopped = min(stops, key=lambda leg: leg.exit_ts)
        survivor = next(leg for leg in legs if leg is not stopped)
        if survivor.exit_ts is not None and survivor.exit_ts <= stopped.exit_ts:
            counts["simultaneous"] += 1
            continue
        pair = pairs.get(result.id)
        entry = _survivor_entry(pair, result, survivor)
        if pair is None or entry is None:
            counts["missing_entry"] += 1
            continue
        conditioned.append(
            _Conditioned(
                pair=pair,
                result=result,
                survivor=survivor,
                first_stop_ts=stopped.exit_ts,
                survivor_entry=entry,
                outcome=pair_outcome(result, pair, engine.params),
            )
        )
    return conditioned, counts


def _survivor_entry(
    pair: Pair | None, result: TradePairResult, survivor: TradePairLeg
) -> float | None:
    if pair is None:
        return None
    side_entry = pair.long_entry if survivor.side == "long" else pair.short_entry
    return side_entry if side_entry is not None else result.entry


def _episode_for(
    item: _Conditioned,
    episodes: list[Episode],
    anchors: list[SessionAnchor],
    params: EngineParams,
) -> Episode | None:
    anchor = next((one for one in anchors if one.name == item.pair.session), None)
    if anchor is None:
        return None
    anchor_ts = session_anchor_ts(anchor, item.result.entry_ts)
    for episode in episodes:
        if episode.session == item.pair.session and episode.anchor_ts == anchor_ts:
            return episode
    return None


def _reach_cells(structures: list[S1Structure]) -> list[S1ReachCell]:
    cells: list[S1ReachCell] = []
    for group_kind, group_key, subset in _groups(structures):
        for horizon in S1_HORIZON_HOURS:
            key = _key(horizon)
            for k in S1_K_VALUES:
                unconditional = sum(
                    1 for item in subset if item.mfe_r_by_horizon.get(key, 0.0) >= k
                )
                survived = sum(
                    1 for item in subset if item.mfe_r_before_lock_by_horizon.get(key, 0.0) >= k
                )
                cells.append(
                    S1ReachCell(
                        group_kind=group_kind,
                        group_key=group_key,
                        horizon_hours=horizon,
                        k=k,
                        unconditional=_rate(unconditional, len(subset)),
                        lock_survived=_rate(survived, len(subset)),
                    )
                )
    return cells


def _excursions(structures: list[S1Structure]) -> list[S1ExcursionCell]:
    cells: list[S1ExcursionCell] = []
    for group_kind, group_key, subset in _groups(structures):
        for horizon in S1_HORIZON_HOURS:
            key = _key(horizon)
            mfe_r = [item.mfe_r_by_horizon.get(key, 0.0) for item in subset]
            mae_r = [item.mae_r_by_horizon.get(key, 0.0) for item in subset]
            mfe_pips = [value * item.s_pips for value, item in zip(mfe_r, subset, strict=True)]
            mae_pips = [value * item.s_pips for value, item in zip(mae_r, subset, strict=True)]
            mfe_orb = [
                value * item.s_pips / item.orb_range_pips
                for value, item in zip(mfe_r, subset, strict=True)
                if item.orb_range_pips
            ]
            mae_orb = [
                value * item.s_pips / item.orb_range_pips
                for value, item in zip(mae_r, subset, strict=True)
                if item.orb_range_pips
            ]
            cells.append(
                S1ExcursionCell(
                    group_kind=group_kind,
                    group_key=group_key,
                    horizon_hours=horizon,
                    n=len(subset),
                    mfe_pips_median=_median(mfe_pips),
                    mfe_pips_p95=_percentile(mfe_pips, 0.95),
                    mfe_pips_mean=_mean(mfe_pips),
                    mae_pips_median=_median(mae_pips),
                    mae_pips_p95=_percentile(mae_pips, 0.95),
                    mae_pips_mean=_mean(mae_pips),
                    mfe_r_median=_median(mfe_r),
                    mfe_r_p95=_percentile(mfe_r, 0.95),
                    mae_r_median=_median(mae_r),
                    mae_r_p95=_percentile(mae_r, 0.95),
                    mfe_orb_units_median=_median(mfe_orb),
                    mfe_orb_units_p95=_percentile(mfe_orb, 0.95),
                    mae_orb_units_median=_median(mae_orb),
                    mae_orb_units_p95=_percentile(mae_orb, 0.95),
                )
            )
    return cells


def _groups(
    structures: list[S1Structure],
) -> list[tuple[str, str, list[S1Structure]]]:
    groups: list[tuple[str, str, list[S1Structure]]] = [("all", "all", structures)]
    for session in sorted({item.session for item in structures}):
        groups.append(
            ("session", session, [item for item in structures if item.session == session])
        )
    for tercile in ("low", "mid", "high", "unclassified"):
        subset = [item for item in structures if item.atr_tercile == tercile]
        if subset:
            groups.append(("atr_tercile", tercile, subset))
    return groups


def _rate(reached: int, n: int) -> ReachRate:
    interval = wilson_interval(reached, n)
    return ReachRate(
        reached=reached,
        n=n,
        rate=reached / n if n else None,
        ci_low=interval[0] if interval else None,
        ci_high=interval[1] if interval else None,
    )


def _key(horizon: float) -> str:
    return f"{horizon:g}h"


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return float(fmean(values)) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def render_s1_markdown(report: S1TargetHitReport) -> str:
    """Render every reach cell and excursion row. Nothing is filtered to a headline."""
    lines = [
        "# S1 conditional target-hit",
        "",
        "`P(survivor reaches kR within H | the first stop occurred)`, by session, holding "
        "horizon and ATR regime tercile, plus the MFE and MAE distributions that go with it. "
        "This is the study that selects `RR`, so reach is measured **beyond** the configured "
        f"target: the reference run used `RR={report.shared_params.get('rr')}`, and reach at "
        "`k` above that is measured on the forward path rather than on realised exits.",
        "",
        "Two columns are reported for every cell. **Unconditional** credits any bar extreme "
        "within the horizon. **Lock-survived** credits only extremes reached before the bar "
        "that first touches the lock; where one bar touches both, the lock is taken first. "
        "Both are upper bounds on what a live path would have delivered, because a completed "
        "bar's high and low are credited to the whole bar.",
        "",
    ]
    lines += markdown.identity_section(
        report,
        extra=[
            ("Reference entry mode", report.reference_entry_mode.value),
            ("Sessions", ", ".join(report.sessions)),
            ("k values", ", ".join(f"{k:g}" for k in report.k_values)),
            ("Horizons (hours)", ", ".join(f"{h:g}" for h in report.horizon_hours)),
            ("ATR period (bars)", str(report.atr_period)),
            (
                "ATR tercile edges (pips)",
                ", ".join(markdown.num(edge) for edge in report.atr_tercile_edges_pips) or "—",
            ),
        ],
    )
    lines += markdown.m1_section(report.m1_coverage)
    lines += _conditioning_section(report)
    lines += _reach_section(report)
    lines += _excursion_section(report)
    lines += _structure_section(report)
    lines += _s1_caveats(report)
    return "\n".join(lines).rstrip() + "\n"


def _conditioning_section(report: S1TargetHitReport) -> list[str]:
    summary = report.conditioning
    lines = [
        "## Conditioning",
        "",
        "The conditioned sample is every structure in which exactly one leg was stopped "
        "before the other leg exited. Exclusions are counted here rather than dropped "
        "silently.",
        "",
    ]
    lines += markdown.table(
        ["Field", "Count"],
        [
            ["Structures in the reference run", str(summary.structures_total)],
            ["Conditioned (first stop occurred)", str(summary.conditioned)],
            ["Excluded: no leg stopped", str(summary.excluded_no_stop)],
            ["Excluded: both legs stopped together", str(summary.excluded_simultaneous_stop)],
            ["Excluded: not a two-leg structure", str(summary.excluded_not_two_legs)],
            ["Excluded: survivor entry unavailable", str(summary.excluded_missing_entry)],
            ["Excluded: no forward bars after the stop", str(summary.excluded_no_forward_bars)],
            ["Survivors whose lock level was touched", str(summary.lock_touched)],
            ["Lock distance (pips)", markdown.num(summary.lock_distance_pips)],
            ["Locks collapsed to entry (S < lock)", str(summary.lock_collapsed_to_entry)],
        ],
    )
    return lines


def _reach_section(report: S1TargetHitReport) -> list[str]:
    rows = [
        [
            cell.group_kind,
            cell.group_key,
            f"{cell.horizon_hours:g}",
            f"{cell.k:g}",
            str(cell.unconditional.n),
            str(cell.unconditional.reached),
            markdown.pct(cell.unconditional.rate),
            markdown.pct(cell.unconditional.ci_low),
            markdown.pct(cell.unconditional.ci_high),
            str(cell.lock_survived.reached),
            markdown.pct(cell.lock_survived.rate),
            markdown.pct(cell.lock_survived.ci_low),
            markdown.pct(cell.lock_survived.ci_high),
        ]
        for cell in report.reach_cells
    ]
    return [
        "## Conditional reach, every cell",
        "",
    ] + markdown.table(
        [
            "Group",
            "Key",
            "Horizon h",
            "k",
            "n",
            "Reached",
            "Rate",
            "CI low",
            "CI high",
            "Reached before lock",
            "Rate",
            "CI low",
            "CI high",
        ],
        rows,
        align_right_from=2,
    )


def _excursion_section(report: S1TargetHitReport) -> list[str]:
    rows = [
        [
            cell.group_kind,
            cell.group_key,
            f"{cell.horizon_hours:g}",
            str(cell.n),
            markdown.num(cell.mfe_pips_median),
            markdown.num(cell.mfe_pips_p95),
            markdown.num(cell.mfe_pips_mean),
            markdown.num(cell.mae_pips_median),
            markdown.num(cell.mae_pips_p95),
            markdown.num(cell.mae_pips_mean),
            markdown.num(cell.mfe_r_median, 4),
            markdown.num(cell.mfe_r_p95, 4),
            markdown.num(cell.mae_r_median, 4),
            markdown.num(cell.mae_r_p95, 4),
            markdown.num(cell.mfe_orb_units_median, 4),
            markdown.num(cell.mfe_orb_units_p95, 4),
            markdown.num(cell.mae_orb_units_median, 4),
            markdown.num(cell.mae_orb_units_p95, 4),
        ]
        for cell in report.excursions
    ]
    return [
        "## MFE and MAE distributions",
        "",
        "Opening-range units divide the excursion by that structure's own opening range, so "
        "`1.0` means the survivor travelled exactly one opening range.",
        "",
    ] + markdown.table(
        [
            "Group",
            "Key",
            "Horizon h",
            "n",
            "MFE pips p50",
            "MFE pips p95",
            "MFE pips mean",
            "MAE pips p50",
            "MAE pips p95",
            "MAE pips mean",
            "MFE R p50",
            "MFE R p95",
            "MAE R p50",
            "MAE R p95",
            "MFE ORB p50",
            "MFE ORB p95",
            "MAE ORB p50",
            "MAE ORB p95",
        ],
        rows,
        align_right_from=2,
    )


def _structure_section(report: S1TargetHitReport) -> list[str]:
    longest = _key(max(report.horizon_hours))
    rows = [
        [
            item.pair_id,
            item.session,
            item.survivor_side,
            markdown.ts(item.first_stop_ts),
            markdown.num(item.s_pips),
            markdown.num(item.orb_range_pips),
            markdown.num(item.atr_pips),
            item.atr_tercile,
            markdown.num(item.lock_price, 3),
            markdown.ts(item.lock_touched_ts),
            str(item.forward_bars),
            markdown.num(item.mfe_r_by_horizon.get(longest), 4),
            markdown.num(item.mae_r_by_horizon.get(longest), 4),
            markdown.num(item.mfe_r_before_lock_by_horizon.get(longest), 4),
            item.realized_outcome,
        ]
        for item in report.structures
    ]
    return [
        "## Every conditioned structure",
        "",
        f"Excursions shown at the longest horizon ({longest}); the JSON carries all horizons.",
        "",
    ] + markdown.table(
        [
            "Pair",
            "Session",
            "Survivor",
            "First stop",
            "S pips",
            "ORB pips",
            "ATR pips",
            "ATR tercile",
            "Lock price",
            "Lock touched",
            "Fwd bars",
            f"MFE R {longest}",
            f"MAE R {longest}",
            f"MFE R before lock {longest}",
            "Realised outcome",
        ],
        rows,
        align_right_from=3,
    )


def _s1_caveats(report: S1TargetHitReport) -> list[str]:
    return [
        "## Caveats",
        "",
        f"- The conditioned sample is {report.conditioning.conditioned} structures from "
        f"{report.bar_count} {report.timeframe.value} bars between "
        f"{report.first_bar_ts.isoformat()} and {report.last_bar_ts.isoformat()}. Session and "
        "tercile subgroups are smaller still; read the confidence intervals, not the point "
        "estimates.",
        f"- {report.m1_coverage.fallback_description}",
        "- Reach frequencies are **upper bounds**. A completed bar's extreme is credited to the "
        "whole bar, so a level touched at any point inside a bar counts as reached even when a "
        "live path would have been stopped out first.",
        "- The stop bar itself is excluded from the forward walk, because without M1 there is no "
        "way to know which part of that bar came after the stop.",
        "- This study selects no `RR` and changes no parameter. It reports what the survivor did; "
        "choosing a target from it is a Phase 3 decision gated by §9.",
    ]
