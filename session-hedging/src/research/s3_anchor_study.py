"""S3: anchor study.

The pilot found New York negative and simultaneously flagged that the New York anchor
may be economically wrong. This runs the identical strategy across the v3 §4.1 anchor
grid — one anchor at a time, as the only session, everything else held fixed — and
reports expectancy beside the range and tick-volume expansion each anchor produces
against the equal-length window that precedes it.

An anchor that does not expand range or volume is not marking an event. An anchor that
expands both and still loses is marking a real event the strategy cannot trade.
"""

from __future__ import annotations

from statistics import median
from typing import Literal

from anchors import SessionAnchor, parse_anchor
from cell_stats import candle_sha256, completed_structures, shared_cell_metrics
from engine import ClosedBarEngine
from models import (
    Candle,
    EngineParams,
    S3AnchorCell,
    S3AnchorStudyReport,
    Timeframe,
)
from research import markdown
from research.episodes import build_episodes
from research.scale import m1_coverage
from sessions import SessionWindow, build_windows

# (session, label, TZ:HH:MM, incumbent, basis) — v3 §4.1, unchanged from v2 §3.2.
# Sydney is deliberately absent: no gold exchange open exists for it.
S3_ANCHOR_GRID: tuple[tuple[str, str, str, bool, str], ...] = (
    ("tokyo", "tokyo_0900", "Asia/Tokyo:09:00", True, "incumbent"),
    ("tokyo", "tokyo_0845", "Asia/Tokyo:08:45", False, "JPX/TOCOM gold day session open"),
    ("london", "london_0800", "Europe/London:08:00", True, "LBMA market-making hours begin"),
    ("london", "london_1030", "Europe/London:10:30", False, "LBMA Gold Price AM auction"),
    ("london", "london_1500", "Europe/London:15:00", False, "LBMA Gold Price PM auction"),
    ("new_york", "new_york_0800", "America/New_York:08:00", True, "incumbent, and the suspect"),
    (
        "new_york",
        "new_york_0820",
        "America/New_York:08:20",
        False,
        "COMEX open-outcry reference",
    ),
    ("new_york", "new_york_0830", "America/New_York:08:30", False, "US tier-1 data window"),
    ("new_york", "new_york_0930", "America/New_York:09:30", False, "US equity open"),
)


def run_s3_anchor_study(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> S3AnchorStudyReport:
    """Run every anchor variant on one candle set with one shared configuration."""
    if not candles:
        raise ValueError("S3 requires at least one candle")

    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []
    window_by_name = {window.name: window for window in windows}

    cells: list[S3AnchorCell] = []
    for session, label, spec, incumbent, basis in S3_ANCHOR_GRID:
        anchor = parse_anchor(session, spec)
        window = window_by_name.get(session) or build_windows([session], {})[0]
        cells.append(
            _cell(
                candles,
                window=window,
                anchor=anchor,
                params=params,
                m1_bars=subpath_bars,
                label=label,
                spec=spec,
                incumbent=incumbent,
                basis=basis,
                symbol=symbol,
                timeframe=timeframe,
                source=source,
            )
        )

    return S3AnchorStudyReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        shared_params=params.model_dump(mode="json"),
        entry_mode=params.entry_mode,
        m1_coverage=coverage,
        expansion_baseline="equal_length_window_before_the_anchor",
        cells=cells,
    )


def _cell(
    candles: list[Candle],
    *,
    window: SessionWindow,
    anchor: SessionAnchor,
    params: EngineParams,
    m1_bars: list[Candle],
    label: str,
    spec: str,
    incumbent: bool,
    basis: str,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
) -> S3AnchorCell:
    engine = ClosedBarEngine([window], params, [anchor], m1_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(
        update={"bar_count": len(candles)}
    )
    metrics = shared_cell_metrics(engine, report, completed_structures(engine, report))
    episodes = build_episodes(candles, [anchor], params)
    stats = next(
        (row for row in report.session_anchor_stats if row.session == anchor.name), None
    )
    ranges = [episode.orb_range_pips for episode in episodes]
    range_expansions = [
        episode.range_expansion for episode in episodes if episode.range_expansion is not None
    ]
    volume_expansions = [
        episode.volume_expansion for episode in episodes if episode.volume_expansion is not None
    ]

    return S3AnchorCell(
        session=anchor.name,
        anchor_label=label,
        anchor_spec=spec,
        is_incumbent=incumbent,
        basis=basis,
        signals=sum(1 for event in engine.events if event.kind == "signal"),
        anchor_skips=stats.skip_count if stats else 0,
        anchor_drift_p50=stats.anchor_drift_p50 if stats else None,
        anchor_drift_max=stats.anchor_drift_max if stats else None,
        episodes=len(episodes),
        completed_structures=int(metrics["completed_structures"]),
        gross_pips=float(metrics["gross_pips"]),
        net_pips=float(metrics["net_pips"]),
        gross_r=float(metrics["gross_r"]),
        net_r=float(metrics["net_r"]),
        gross_expectancy_pips=metrics["gross_expectancy_pips"],
        net_expectancy_pips=metrics["net_expectancy_pips"],
        gross_expectancy_r=metrics["gross_expectancy_r"],
        net_expectancy_r=metrics["net_expectancy_r"],
        gross_profit_factor=metrics["gross_profit_factor"],
        net_profit_factor=metrics["net_profit_factor"],
        survivor_tp_rate=report.survivor_tp_rate,
        breakeven_tp_rate_required=report.breakeven_tp_rate_required,
        tp_rate_margin_pp=report.tp_rate_margin_pp,
        tp_rate_margin_pp_ci_low=report.tp_rate_margin_pp_ci_low,
        tp_rate_margin_pp_ci_high=report.tp_rate_margin_pp_ci_high,
        gross_max_drawdown_r=report.gross_max_drawdown_r,
        net_max_drawdown_r=report.net_max_drawdown_r,
        median_orb_range_pips=float(median(ranges)) if ranges else None,
        median_range_expansion=float(median(range_expansions)) if range_expansions else None,
        median_volume_expansion=float(median(volume_expansions)) if volume_expansions else None,
        range_expansion_episodes=len(range_expansions),
        volume_expansion_episodes=len(volume_expansions),
        suppressed_signals=int(metrics["suppressed_signals"]),
        unresolved_structures=int(metrics["unresolved_structures"]),
        prop_guard_breached=bool(metrics["prop_guard_breached"]),
    )


def render_s3_markdown(report: S3AnchorStudyReport) -> str:
    """Every anchor variant, incumbent and alternative alike, in one table."""
    lines = [
        "# S3 anchor study",
        "",
        "The v3 §4.1 anchor grid, one anchor at a time, as the only session, on one candle "
        "set with one configuration. Expansion ratios compare the opening range and its tick "
        "volume with the equal-length window immediately before the anchor: a ratio near "
        "`1.0` means the anchor marks nothing in particular.",
        "",
        "**Primary question**: is New York's negative result an anchor problem? The four New "
        "York rows below answer it descriptively for this window. No anchor is selected here.",
        "",
    ]
    lines += markdown.identity_section(
        report,
        extra=[
            ("Entry mode", report.entry_mode.value),
            ("Anchor variants", str(len(report.cells))),
            ("Expansion baseline", report.expansion_baseline),
        ],
    )
    lines += markdown.m1_section(report.m1_coverage)
    rows = [
        [
            cell.session,
            cell.anchor_label,
            f"`{cell.anchor_spec}`",
            "yes" if cell.is_incumbent else "no",
            cell.basis,
            str(cell.signals),
            str(cell.anchor_skips),
            markdown.num(cell.anchor_drift_p50),
            str(cell.completed_structures),
            markdown.num(cell.gross_pips),
            markdown.num(cell.net_pips),
            markdown.num(cell.gross_r, 4),
            markdown.num(cell.net_r, 4),
            markdown.num(cell.gross_expectancy_pips),
            markdown.num(cell.net_expectancy_pips),
            markdown.num(cell.gross_expectancy_r, 4),
            markdown.num(cell.net_expectancy_r, 4),
            markdown.num(cell.gross_profit_factor, 4),
            markdown.num(cell.net_profit_factor, 4),
            markdown.pct(cell.survivor_tp_rate),
            markdown.pct(cell.breakeven_tp_rate_required),
            markdown.num(cell.tp_rate_margin_pp),
            markdown.num(cell.tp_rate_margin_pp_ci_low),
            markdown.num(cell.tp_rate_margin_pp_ci_high),
            markdown.num(cell.net_max_drawdown_r, 4),
            markdown.num(cell.median_orb_range_pips),
            markdown.num(cell.median_range_expansion, 4),
            markdown.num(cell.median_volume_expansion, 4),
            str(cell.suppressed_signals),
            str(cell.unresolved_structures),
        ]
        for cell in report.cells
    ]
    lines += ["## Every anchor variant", ""]
    lines += markdown.table(
        [
            "Session", "Anchor", "Spec", "Incumbent", "Basis", "Signals", "Drift skips",
            "Drift p50 min", "Completed", "Gross pips", "Net pips", "Gross R", "Net R",
            "Gross exp pips", "Net exp pips", "Gross exp R", "Net exp R", "Gross PF", "Net PF",
            "Survivor TP", "Required TP", "Margin pp", "CI low", "CI high", "Net maxDD R",
            "Median ORB pips", "Median range expansion", "Median volume expansion",
            "Suppressed", "Unresolved",
        ],
        rows,
        align_right_from=5,
    )
    lines += _s3_degeneracy(report)
    lines += _s3_reading(report)
    return "\n".join(lines).rstrip() + "\n"


def _s3_degeneracy(report: S3AnchorStudyReport) -> list[str]:
    """An anchor off a bar boundary snaps forward, so some variants are the same run."""
    groups: dict[tuple[object, ...], list[S3AnchorCell]] = {}
    for cell in report.cells:
        key = (
            cell.session,
            cell.signals,
            cell.completed_structures,
            cell.gross_r,
            cell.net_r,
            cell.median_orb_range_pips,
        )
        groups.setdefault(key, []).append(cell)
    collapsed = [group for group in groups.values() if len(group) > 1]
    lines = [
        "## Bar-resolution degeneracy",
        "",
        "An anchor that does not fall on a bar boundary snaps forward to the next bar open, "
        "so two anchors inside the same bar are the same experiment at this resolution. "
        "Variants whose measured results are identical are listed here; treat them as one "
        "observation, not as agreement between two anchors.",
        "",
    ]
    if not collapsed:
        lines += ["No anchor variants collapsed on this candle set.", ""]
        return lines
    lines += markdown.table(
        ["Session", "Collapsed variants", "Signals", "Completed", "Net R"],
        [
            [
                group[0].session,
                ", ".join(cell.anchor_label for cell in group),
                str(group[0].signals),
                str(group[0].completed_structures),
                markdown.num(group[0].net_r, 4),
            ]
            for group in collapsed
        ],
        align_right_from=2,
    )
    return lines


def _s3_reading(report: S3AnchorStudyReport) -> list[str]:
    ny = [cell for cell in report.cells if cell.session == "new_york"]
    incumbent = next((cell for cell in ny if cell.is_incumbent), None)
    lines = [
        "## Reading the New York question",
        "",
    ]
    if incumbent is None:
        lines.append("No New York incumbent row was produced on this candle set.")
        return lines
    better = [
        cell for cell in ny if not cell.is_incumbent and cell.net_r > incumbent.net_r
    ]
    lines += [
        f"- The incumbent New York anchor (`{incumbent.anchor_spec}`) produced "
        f"{incumbent.completed_structures} completed structures, "
        f"{markdown.num(incumbent.net_r, 4)} net R, and a median range expansion of "
        f"{markdown.num(incumbent.median_range_expansion, 4)}.",
        f"- {len(better)} of the {len(ny) - 1} alternative New York anchors finished above it in "
        "net R on this window. That is a description of one month, not a reason to move the "
        "anchor; §9 requires walk-forward evidence before an anchor changes.",
        "- Every anchor's drift statistics are reported above. An anchor whose p50 drift "
        "exceeds `ANCHOR_TOLERANCE_MINUTES` would be void rather than underperforming, which "
        "is the H4 lesson from §0.2.",
        "",
        "## Caveats",
        "",
        f"- {report.bar_count} {report.timeframe.value} bars from "
        f"{report.first_bar_ts.isoformat()} to {report.last_bar_ts.isoformat()}: roughly twenty "
        "trading days per anchor. Every row here is a small sample.",
        f"- {report.m1_coverage.fallback_description}",
        "- Each variant runs as the only session, so concurrency and the one-open-per-session "
        "gate cannot interact across sessions. That isolates the anchor and makes these rows "
        "incomparable with a three-session run.",
        "- Expansion ratios use tick volume as reported by the data provider. They describe "
        "activity around the anchor, not spread or liquidity, which the local cache does not "
        "carry.",
    ]
    return lines
