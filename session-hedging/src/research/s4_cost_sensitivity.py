"""S4: cost sensitivity and break-even, in pips per side.

The pilot's cost budget was about 1.0x its modelled cost. §9 requires 2x headroom before
release, so the useful question is not "what does it earn at zero cost" but "at what cost
per side does each mode stop working". This sweeps spread, slippage and commission
independently, per entry mode, on one candle set, and reports the break-even cost per
completed side beside the configured cost that produced it.
"""

from __future__ import annotations

from itertools import product
from typing import Literal

from anchors import SessionAnchor
from cell_stats import candle_sha256, completed_structures, shared_cell_metrics
from engine import ClosedBarEngine
from models import (
    Candle,
    EngineParams,
    EntryMode,
    S4CostCell,
    S4CostSensitivityReport,
    Timeframe,
)
from research import markdown
from research.scale import m1_coverage
from sessions import SessionWindow

S4_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
S4_SPREAD_GRID: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 6.0)
S4_SLIPPAGE_GRID: tuple[float, ...] = (0.0, 0.5, 1.0)
S4_COMMISSION_GRID: tuple[float, ...] = (0.0, 0.5)
S4_HEADROOM_GATE = 2.0
S4_CELL_COUNT = (
    len(S4_MODES) * len(S4_SPREAD_GRID) * len(S4_SLIPPAGE_GRID) * len(S4_COMMISSION_GRID)
)


def run_s4_cost_sensitivity(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> S4CostSensitivityReport:
    """Sweep the cost surface per mode. Only the three cost fields vary."""
    if not candles:
        raise ValueError("S4 requires at least one candle")

    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []

    cells: list[S4CostCell] = []
    for mode, spread, slippage, commission in product(
        S4_MODES, S4_SPREAD_GRID, S4_SLIPPAGE_GRID, S4_COMMISSION_GRID
    ):
        cell_params = EngineParams.model_validate(
            params.model_dump()
            | {
                "entry_mode": mode,
                "spread_pips_per_side": spread,
                "slippage_pips_per_side": slippage,
                "commission_pips_per_side": commission,
            }
        )
        engine = ClosedBarEngine(windows, cell_params, anchors, subpath_bars)
        engine.run(candles)
        report = engine.report(symbol, timeframe, source).model_copy(
            update={"bar_count": len(candles)}
        )
        metrics = shared_cell_metrics(engine, report, completed_structures(engine, report))
        headroom = report.cost_headroom_ratio
        cells.append(
            S4CostCell(
                entry_mode=mode,
                spread_pips_per_side=spread,
                slippage_pips_per_side=slippage,
                commission_pips_per_side=commission,
                configured_execution_cost_pips_per_side=(
                    report.configured_execution_cost_pips_per_side
                ),
                completed_structures=int(metrics["completed_structures"]),
                gross_pips=float(metrics["gross_pips"]),
                net_pips=float(metrics["net_pips"]),
                gross_r=float(metrics["gross_r"]),
                net_r=float(metrics["net_r"]),
                execution_cost_pips=float(metrics["execution_cost_pips"]),
                financing_cost_pips=float(metrics["financing_cost_pips"]),
                total_cost_pips=float(metrics["total_cost_pips"]),
                gross_expectancy_pips=metrics["gross_expectancy_pips"],
                net_expectancy_pips=metrics["net_expectancy_pips"],
                gross_expectancy_r=metrics["gross_expectancy_r"],
                net_expectancy_r=metrics["net_expectancy_r"],
                gross_profit_factor=metrics["gross_profit_factor"],
                net_profit_factor=metrics["net_profit_factor"],
                transaction_sides=int(metrics["transaction_sides"]),
                cost_side_equivalents=float(metrics["cost_side_equivalents"]),
                breakeven_pips_per_completed_side=metrics["breakeven_pips_per_completed_side"],
                cost_headroom_ratio=headroom,
                meets_two_times_headroom=headroom is not None and headroom >= S4_HEADROOM_GATE,
                net_pips_positive=float(metrics["net_pips"]) > 0,
                net_r_positive=float(metrics["net_r"]) > 0,
                pips_and_r_agree_in_sign=(
                    (float(metrics["net_pips"]) > 0) == (float(metrics["net_r"]) > 0)
                ),
            )
        )

    return S4CostSensitivityReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        shared_params=_shared(params),
        entry_modes=list(S4_MODES),
        spread_grid=list(S4_SPREAD_GRID),
        slippage_grid=list(S4_SLIPPAGE_GRID),
        commission_grid=list(S4_COMMISSION_GRID),
        expected_cell_count=S4_CELL_COUNT,
        headroom_gate=S4_HEADROOM_GATE,
        m1_coverage=coverage,
        cells=cells,
    )


def _shared(params: EngineParams) -> dict[str, object]:
    shared = params.model_dump(mode="json")
    for field in (
        "entry_mode",
        "spread_pips_per_side",
        "slippage_pips_per_side",
        "commission_pips_per_side",
    ):
        shared.pop(field, None)
    return shared


def render_s4_markdown(report: S4CostSensitivityReport) -> str:
    """Every cost cell for every mode, including the ones that never clear the gate."""
    lines = [
        "# S4 cost sensitivity and break-even",
        "",
        f"`{len(report.entry_modes)} modes x {len(report.spread_grid)} spreads x "
        f"{len(report.slippage_grid)} slippage values x {len(report.commission_grid)} "
        f"commissions = {report.expected_cell_count}` cells, of which {len(report.cells)} are "
        "reported. Only the three cost fields and the entry mode vary.",
        "",
        f"`cost_headroom_ratio` is the break-even cost per completed side divided by the "
        f"configured execution cost per side. §9 requires **{report.headroom_gate:g}x** before "
        "release. A cell with no positive gross edge has no headroom to report, which is not "
        "the same as failing narrowly.",
        "",
        "`Meets 2x` and the two `Net ... positive` columns can disagree, and the "
        "disagreement is not a bug: "
        "break-even pips per side is computed from completed structures, while net pips and "
        "net R are final marked equity including structures still open at the end of the "
        "window. Read them together, never one alone.",
        "",
        "Net pips and net R are reported separately because they can disagree in sign, which "
        "is §0.7's point restated on this window: a cell can end positive in pips while every "
        "R-denominated reading of it is negative, because R normalises each structure by its "
        "own stop distance.",
        "",
    ]
    lines += markdown.identity_section(
        report,
        extra=[
            ("Entry modes", ", ".join(mode.value for mode in report.entry_modes)),
            ("Spread grid (pips/side)", ", ".join(f"{v:g}" for v in report.spread_grid)),
            ("Slippage grid (pips/side)", ", ".join(f"{v:g}" for v in report.slippage_grid)),
            ("Commission grid (pips/side)", ", ".join(f"{v:g}" for v in report.commission_grid)),
            ("Headroom gate", f"{report.headroom_gate:g}x"),
        ],
    )
    lines += markdown.m1_section(report.m1_coverage)
    rows = [
        [
            cell.entry_mode.value,
            f"{cell.spread_pips_per_side:g}",
            f"{cell.slippage_pips_per_side:g}",
            f"{cell.commission_pips_per_side:g}",
            markdown.num(cell.configured_execution_cost_pips_per_side, 4),
            str(cell.completed_structures),
            markdown.num(cell.gross_pips),
            markdown.num(cell.net_pips),
            markdown.num(cell.gross_r, 4),
            markdown.num(cell.net_r, 4),
            markdown.num(cell.execution_cost_pips),
            markdown.num(cell.financing_cost_pips),
            markdown.num(cell.gross_expectancy_pips),
            markdown.num(cell.net_expectancy_pips),
            markdown.num(cell.gross_expectancy_r, 4),
            markdown.num(cell.net_expectancy_r, 4),
            markdown.num(cell.gross_profit_factor, 4),
            markdown.num(cell.net_profit_factor, 4),
            str(cell.transaction_sides),
            markdown.num(cell.cost_side_equivalents),
            markdown.num(cell.breakeven_pips_per_completed_side, 4),
            markdown.num(cell.cost_headroom_ratio, 4),
            "yes" if cell.meets_two_times_headroom else "no",
            "yes" if cell.net_pips_positive else "no",
            "yes" if cell.net_r_positive else "no",
        ]
        for cell in report.cells
    ]
    lines += ["## Every cost cell", ""]
    lines += markdown.table(
        [
            "Mode", "Spread", "Slippage", "Commission", "Configured cost/side", "Completed",
            "Gross pips", "Net pips", "Gross R", "Net R", "Execution cost", "Financing cost",
            "Gross exp pips", "Net exp pips", "Gross exp R", "Net exp R", "Gross PF", "Net PF",
            "Sides", "Weighted sides", "Break-even pips/side", "Headroom", "Meets 2x",
            "Net pips positive", "Net R positive",
        ],
        rows,
        align_right_from=4,
    )
    lines += _s4_summary(report)
    return "\n".join(lines).rstrip() + "\n"


def _s4_summary(report: S4CostSensitivityReport) -> list[str]:
    disagreeing = [cell for cell in report.cells if not cell.pips_and_r_agree_in_sign]
    lines = [
        "## Where each mode stops working",
        "",
        f"**Net pips and net R disagree in sign in {len(disagreeing)} of {len(report.cells)} "
        "cells.** That is not a rounding artefact: pips weight every structure equally while R "
        "divides each by its own stop, so a few wide-stop winners can carry the pip total while "
        "the R total stays negative. Neither column is the answer on its own.",
        "",
    ]
    rows = []
    for mode in report.entry_modes:
        cells = [cell for cell in report.cells if cell.entry_mode is mode]
        zero_cost = next(
            (
                cell
                for cell in cells
                if cell.configured_execution_cost_pips_per_side == 0.0
            ),
            None,
        )
        positive = [cell for cell in cells if cell.net_pips_positive]
        highest_positive = (
            max(cell.configured_execution_cost_pips_per_side for cell in positive)
            if positive
            else None
        )
        rows.append(
            [
                mode.value,
                str(len(cells)),
                markdown.num(zero_cost.net_r, 4) if zero_cost else "—",
                str(len(positive)),
                markdown.num(highest_positive, 4) if highest_positive is not None else "—",
                str(sum(1 for cell in cells if cell.meets_two_times_headroom)),
                markdown.num(
                    max(
                        (
                            cell.cost_headroom_ratio
                            for cell in cells
                            if cell.cost_headroom_ratio is not None
                        ),
                        default=None,
                    ),
                    4,
                ),
            ]
        )
    lines += markdown.table(
        [
            "Mode", "Cells", "Net R at zero cost", "Cells net-pips positive",
            "Highest cost/side still net-pips positive",
            f"Cells meeting {report.headroom_gate:g}x", "Best headroom",
        ],
        rows,
    )
    lines += [
        "## Caveats",
        "",
        f"- {report.bar_count} {report.timeframe.value} bars from "
        f"{report.first_bar_ts.isoformat()} to {report.last_bar_ts.isoformat()}. A cost ladder "
        "on one month cannot establish a cost budget; it shows how quickly each mode's edge "
        "is consumed on this window.",
        f"- {report.m1_coverage.fallback_description}",
        "- Costs here are **modelled**, not measured. §12 is explicit that broker bid/ask ticks "
        "must replace these assumptions before any cost conclusion is final, and the local "
        "cache carries no spread column.",
        "- Financing is swept only through the configured swap rates, which remain at their "
        "configured values in every cell; the ladder varies execution cost only.",
        "- No mode is selected and no cost budget is adopted here.",
    ]
    return lines
