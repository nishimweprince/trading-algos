"""S2: single-break versus double-break frequency.

Once the opening range is established, how often does price leave one side and never
come back through the other? That single number prices the `-2R` whipsaw for
``hedge_pair`` and the false break for ``oco_bracket``, so it is measured on the price
path first and then checked against what the engine actually paid.

The walk starts when the opening range closes, not at the entry time, so the answer
describes the range rather than the entry delay. With no covering M1 subpath a bar that
breaks both sides is reported as its own ambiguous class rather than being guessed.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Literal

from anchors import SessionAnchor
from cell_stats import candle_sha256, pair_outcome
from engine import ClosedBarEngine
from metrics import wilson_interval
from models import (
    Candle,
    EngineParams,
    EntryMode,
    S2BreakFrequencyReport,
    S2Cell,
    S2Episode,
    S2ModeCompanion,
    Timeframe,
)
from research import markdown
from research.episodes import Episode, build_episodes, tercile_edges, tercile_label
from research.scale import m1_coverage
from sessions import SessionWindow

S2_HORIZON_HOURS: tuple[float, ...] = (4.0, 8.0, 12.0, 24.0, 48.0)
S2_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
FALSE_BREAK_DEFINITION = (
    "triggered structures that closed at a loss, divided by triggered structures; "
    "entry orders that expired or were cancelled without triggering are excluded"
)


def run_s2_break_frequency(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> S2BreakFrequencyReport:
    """Classify every session-day range, then price it against all four entry modes."""
    if not candles:
        raise ValueError("S2 requires at least one candle")

    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []
    episodes = build_episodes(candles, anchors, params)
    ratios = [
        episode.orb_range_pips / episode.atr_pips
        for episode in episodes
        if episode.atr_pips
    ]
    edges = tercile_edges(ratios)

    stamps = [candle.ts for candle in candles]
    records: list[S2Episode] = []
    without_forward = 0
    for episode in episodes:
        walks = _classify(episode, candles, stamps, params, edges)
        if not walks:
            without_forward += 1
            continue
        records.extend(walks)

    companions = [
        _companion(
            candles,
            windows,
            params,
            anchors,
            mode=mode,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            m1_bars=subpath_bars,
        )
        for mode in S2_MODES
    ]

    return S2BreakFrequencyReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        shared_params=params.model_dump(mode="json"),
        sessions=[window.name for window in windows],
        horizon_hours=list(S2_HORIZON_HOURS),
        walk_starts_at="opening_range_close",
        contraction_tercile_edges=list(edges) if edges else [],
        m1_coverage=coverage,
        episodes_total=len(episodes),
        episodes_without_forward_bars=without_forward,
        cells=_cells(records),
        mode_companions=companions,
        episodes=records,
    )


@dataclass(frozen=True, slots=True)
class _Break:
    classification: str
    first_side: str
    first_hours: float | None
    opposite_hours: float | None
    forward_bars: int


def _classify(
    episode: Episode,
    candles: list[Candle],
    stamps: list[datetime],
    params: EngineParams,
    edges: tuple[float, float] | None,
) -> list[S2Episode]:
    orb_close = episode.anchor_ts + timedelta(minutes=params.orb_minutes)
    start = bisect_left(stamps, orb_close)
    if start >= len(candles):
        return []
    ratio = episode.orb_range_pips / episode.atr_pips if episode.atr_pips else None
    records: list[S2Episode] = []
    for horizon in S2_HORIZON_HOURS:
        outcome = _walk(episode, candles[start:], orb_close, horizon)
        records.append(
            S2Episode(
                session=episode.session,
                anchor_ts=episode.anchor_ts,
                weekday=WEEKDAY_NAMES[episode.anchor_ts.weekday()],
                orb_high=episode.orb_high,
                orb_low=episode.orb_low,
                orb_range_pips=episode.orb_range_pips,
                atr_pips=episode.atr_pips,
                contraction_ratio=ratio,
                contraction_tercile=tercile_label(ratio, edges),
                bullish=episode.bullish,
                horizon_hours=horizon,
                classification=outcome.classification,
                first_break_side=outcome.first_side,
                first_break_hours=outcome.first_hours,
                opposite_break_hours=outcome.opposite_hours,
                forward_bars=outcome.forward_bars,
            )
        )
    return records


def _walk(
    episode: Episode, forward: list[Candle], orb_close: datetime, horizon: float
) -> _Break:
    deadline = orb_close + timedelta(hours=horizon)
    first_side = "none"
    first_hours: float | None = None
    opposite_hours: float | None = None
    bars = 0
    for candle in forward:
        if candle.ts > deadline:
            break
        bars += 1
        up = candle.high > episode.orb_high
        down = candle.low < episode.orb_low
        elapsed = (candle.ts - orb_close).total_seconds() / 3600.0
        if first_side == "none":
            if up and down:
                first_side = "both"
                first_hours = elapsed
                opposite_hours = elapsed
                break
            if up or down:
                first_side = "up" if up else "down"
                first_hours = elapsed
            continue
        if (first_side == "up" and down) or (first_side == "down" and up):
            opposite_hours = elapsed
            break
    if first_side == "none":
        classification = "no_break"
    elif first_side == "both":
        classification = "ambiguous_same_bar"
    elif opposite_hours is None:
        classification = f"single_break_{first_side}"
    else:
        classification = f"double_break_{first_side}_first"
    return _Break(
        classification=classification,
        first_side=first_side,
        first_hours=first_hours,
        opposite_hours=opposite_hours,
        forward_bars=bars,
    )


def _cells(records: list[S2Episode]) -> list[S2Cell]:
    cells: list[S2Cell] = []
    for group_kind, group_key, subset in _groups(records):
        for horizon in S2_HORIZON_HOURS:
            rows = [item for item in subset if item.horizon_hours == horizon]
            counts = {
                name: sum(1 for item in rows if item.classification == name)
                for name in (
                    "no_break",
                    "single_break_up",
                    "single_break_down",
                    "double_break_up_first",
                    "double_break_down_first",
                    "ambiguous_same_bar",
                )
            }
            n = len(rows)
            single = counts["single_break_up"] + counts["single_break_down"]
            double = (
                counts["double_break_up_first"]
                + counts["double_break_down_first"]
                + counts["ambiguous_same_bar"]
            )
            single_ci = wilson_interval(single, n)
            double_ci = wilson_interval(double, n)
            first_hours = [
                item.first_break_hours for item in rows if item.first_break_hours is not None
            ]
            opposite_hours = [
                item.opposite_break_hours
                for item in rows
                if item.opposite_break_hours is not None
            ]
            cells.append(
                S2Cell(
                    group_kind=group_kind,
                    group_key=group_key,
                    horizon_hours=horizon,
                    n=n,
                    no_break=counts["no_break"],
                    single_break_up=counts["single_break_up"],
                    single_break_down=counts["single_break_down"],
                    double_break_up_first=counts["double_break_up_first"],
                    double_break_down_first=counts["double_break_down_first"],
                    ambiguous_same_bar=counts["ambiguous_same_bar"],
                    single_break_rate=single / n if n else None,
                    single_break_ci_low=single_ci[0] if single_ci else None,
                    single_break_ci_high=single_ci[1] if single_ci else None,
                    double_break_rate=double / n if n else None,
                    double_break_ci_low=double_ci[0] if double_ci else None,
                    double_break_ci_high=double_ci[1] if double_ci else None,
                    no_break_rate=counts["no_break"] / n if n else None,
                    median_first_break_hours=float(median(first_hours)) if first_hours else None,
                    median_opposite_break_hours=(
                        float(median(opposite_hours)) if opposite_hours else None
                    ),
                )
            )
    return cells


def _groups(records: list[S2Episode]) -> list[tuple[str, str, list[S2Episode]]]:
    groups: list[tuple[str, str, list[S2Episode]]] = [("all", "all", records)]
    for session in sorted({item.session for item in records}):
        groups.append(("session", session, [x for x in records if x.session == session]))
    for weekday in WEEKDAY_NAMES:
        subset = [x for x in records if x.weekday == weekday]
        if subset:
            groups.append(("weekday", weekday, subset))
    for tercile in ("low", "mid", "high", "unclassified"):
        subset = [x for x in records if x.contraction_tercile == tercile]
        if subset:
            groups.append(("contraction_tercile", tercile, subset))
    return groups


def _companion(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    mode: EntryMode,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle],
) -> S2ModeCompanion:
    mode_params = EngineParams.model_validate(params.model_dump() | {"entry_mode": mode})
    engine = ClosedBarEngine(windows, mode_params, anchors, m1_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(
        update={"bar_count": len(candles)}
    )
    pairs = {pair.id: pair for pair in engine.pairs}
    completed = [result for result in report.trade_pairs if result.status == "closed"]
    outcomes = [pair_outcome(result, pairs[result.id], mode_params) for result in completed]
    counts = {
        kind: outcomes.count(kind)
        for kind in ("tp", "lock", "breakeven", "whipsaw", "time_exit")
    }
    whipsaw_ci = wilson_interval(counts["whipsaw"], len(completed))
    cancelled = [event for event in report.events if event.kind == "entry_order_cancelled"]
    triggered = len(report.trade_pairs)
    losses = sum(
        1
        for result in completed
        if (result.net_pnl_pips if result.net_pnl_pips is not None else result.pnl_pips) < 0
    )
    return S2ModeCompanion(
        entry_mode=mode,
        completed_structures=len(completed),
        whipsaw_structures=counts["whipsaw"],
        whipsaw_rate=counts["whipsaw"] / len(completed) if completed else None,
        whipsaw_ci_low=whipsaw_ci[0] if whipsaw_ci else None,
        whipsaw_ci_high=whipsaw_ci[1] if whipsaw_ci else None,
        tp_structures=counts["tp"],
        lock_structures=counts["lock"],
        breakeven_structures=counts["breakeven"],
        time_exit_structures=counts["time_exit"],
        triggered_entry_orders=triggered,
        cancelled_entry_orders=len(cancelled),
        expired_entry_orders=sum(
            event.detail.get("reason") == "expired" for event in cancelled
        ),
        loss_closed_structures=losses,
        false_break_rate=losses / triggered if triggered else None,
        false_break_definition=FALSE_BREAK_DEFINITION,
        gross_pips=report.gross_equity_pips,
        net_pips=report.net_equity_pips,
        gross_r=report.gross_equity_r,
        net_r=report.net_equity_r,
    )


def render_s2_markdown(report: S2BreakFrequencyReport) -> str:
    """Render every group, horizon and episode. No class is collapsed into another."""
    lines = [
        "# S2 single-break versus double-break frequency",
        "",
        "Once the opening range closes, does price leave one side and never test the other? "
        "The single-break share is the ceiling on any one-sided breakout; the double-break "
        "share is what the `hedge_pair` whipsaw and the `oco_bracket` false break are drawn "
        "from.",
        "",
        "Classes are mutually exclusive and exhaust every episode at every horizon: "
        "`no_break`, `single_break_up`, `single_break_down`, `double_break_up_first`, "
        "`double_break_down_first`, and `ambiguous_same_bar` for a bar that breaks both sides "
        "at once. Ambiguous bars are counted with the double breaks in the double-break rate "
        "and are also reported on their own, because without an M1 subpath there is no honest "
        "way to order the two touches.",
        "",
        "The walk starts when the opening range closes, not at the entry time, so the answer "
        "describes the range rather than `ENTRY_DELAY_MINUTES`.",
        "",
    ]
    lines += markdown.identity_section(
        report,
        extra=[
            ("Sessions", ", ".join(report.sessions)),
            ("Horizons (hours)", ", ".join(f"{h:g}" for h in report.horizon_hours)),
            ("Walk starts at", report.walk_starts_at),
            ("Episodes", str(report.episodes_total)),
            ("Episodes without forward bars", str(report.episodes_without_forward_bars)),
            (
                "Contraction tercile edges (ORB pips / ATR pips)",
                ", ".join(markdown.num(edge, 4) for edge in report.contraction_tercile_edges)
                or "—",
            ),
        ],
    )
    lines += markdown.m1_section(report.m1_coverage)
    lines += _s2_cells_section(report)
    lines += _s2_companion_section(report)
    lines += _s2_episode_section(report)
    lines += _s2_caveats(report)
    return "\n".join(lines).rstrip() + "\n"


def _s2_cells_section(report: S2BreakFrequencyReport) -> list[str]:
    rows = [
        [
            cell.group_kind,
            cell.group_key,
            f"{cell.horizon_hours:g}",
            str(cell.n),
            str(cell.no_break),
            str(cell.single_break_up),
            str(cell.single_break_down),
            str(cell.double_break_up_first),
            str(cell.double_break_down_first),
            str(cell.ambiguous_same_bar),
            markdown.pct(cell.single_break_rate),
            markdown.pct(cell.single_break_ci_low),
            markdown.pct(cell.single_break_ci_high),
            markdown.pct(cell.double_break_rate),
            markdown.pct(cell.double_break_ci_low),
            markdown.pct(cell.double_break_ci_high),
            markdown.pct(cell.no_break_rate),
            markdown.num(cell.median_first_break_hours),
            markdown.num(cell.median_opposite_break_hours),
        ]
        for cell in report.cells
    ]
    return ["## Break classes, every group and horizon", ""] + markdown.table(
        [
            "Group", "Key", "Horizon h", "n", "No break", "Single up", "Single down",
            "Double up first", "Double down first", "Ambiguous same bar",
            "Single rate", "CI low", "CI high", "Double rate", "CI low", "CI high",
            "No-break rate", "Median first break h", "Median opposite break h",
        ],
        rows,
        align_right_from=2,
    )


def _s2_companion_section(report: S2BreakFrequencyReport) -> list[str]:
    rows = [
        [
            item.entry_mode.value,
            str(item.completed_structures),
            str(item.whipsaw_structures),
            markdown.pct(item.whipsaw_rate),
            markdown.pct(item.whipsaw_ci_low),
            markdown.pct(item.whipsaw_ci_high),
            str(item.tp_structures),
            str(item.lock_structures),
            str(item.breakeven_structures),
            str(item.time_exit_structures),
            str(item.triggered_entry_orders),
            str(item.cancelled_entry_orders),
            str(item.expired_entry_orders),
            str(item.loss_closed_structures),
            markdown.pct(item.false_break_rate),
            markdown.num(item.gross_pips),
            markdown.num(item.net_pips),
            markdown.num(item.gross_r, 4),
            markdown.num(item.net_r, 4),
        ]
        for item in report.mode_companions
    ]
    return [
        "## What the engine actually paid for these breaks",
        "",
        f"`false_break_rate` is {FALSE_BREAK_DEFINITION}. It is a proxy, not a claim about "
        "intent: a triggered structure can lose for reasons other than a failed break.",
        "",
        "`Cancelled` counts every `entry_order_cancelled` event, which includes the sibling "
        "order cancelled when the other side of a two-sided entry fills. `Expired` is the "
        "subset that timed out without ever filling, and only that subset means "
        "\"no break arrived in time\".",
        "",
    ] + markdown.table(
        [
            "Mode", "Completed", "Whipsaw", "Whipsaw rate", "CI low", "CI high",
            "TP", "Lock", "Breakeven", "Time exit", "Triggered", "Cancelled", "Expired",
            "Loss-closed", "False-break rate", "Gross pips", "Net pips", "Gross R", "Net R",
        ],
        rows,
    )


def _s2_episode_section(report: S2BreakFrequencyReport) -> list[str]:
    rows = [
        [
            item.session,
            item.anchor_ts.isoformat(),
            item.weekday,
            f"{item.horizon_hours:g}",
            markdown.num(item.orb_range_pips),
            markdown.num(item.atr_pips),
            markdown.num(item.contraction_ratio, 4),
            item.contraction_tercile,
            "up" if item.bullish else "down",
            item.classification,
            item.first_break_side,
            markdown.num(item.first_break_hours),
            markdown.num(item.opposite_break_hours),
            str(item.forward_bars),
        ]
        for item in report.episodes
    ]
    return ["## Every episode at every horizon", ""] + markdown.table(
        [
            "Session", "Anchor", "Weekday", "Horizon h", "ORB pips", "ATR pips",
            "ORB/ATR", "Contraction tercile", "Signal", "Class", "First break",
            "First break h", "Opposite break h", "Fwd bars",
        ],
        rows,
        align_right_from=3,
    )


def _s2_caveats(report: S2BreakFrequencyReport) -> list[str]:
    return [
        "## Caveats",
        "",
        f"- {report.episodes_total} episodes over {report.bar_count} "
        f"{report.timeframe.value} bars. Weekday and tercile subgroups hold roughly a dozen "
        "episodes each; their rates are indicative only, which is what the intervals say.",
        f"- {report.m1_coverage.fallback_description}",
        "- A break is any trade beyond the range extreme by any amount. No buffer is applied, "
        "so these rates are the most generous possible reading of 'a side broke'.",
        "- Episodes repeat across horizons by construction: the same session-day appears once "
        "per horizon, so rows are comparable within a horizon and must not be pooled across "
        "horizons.",
        "- This study selects nothing. It measures how often the second side is tested; "
        "whether that is worth hedging against is a §9 question.",
    ]
