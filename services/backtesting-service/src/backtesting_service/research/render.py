"""Render the S8 surface to Markdown. Every cell is printed, winners and losers alike."""

from __future__ import annotations

from datetime import datetime
from statistics import median

from ..models import ScaleSweepCell, ScaleSweepReport
from .scale import HOLD_BUCKETS

_CORE_HEADER = (
    "| # | Mode | ORB | Delay | MaxAge | Completed | Gross pips | Net pips | Gross R | Net R | "
    "Gross exp pips | Net exp pips | Gross exp R | Net exp R | Gross PF | Net PF | "
    "Gross win excl BE | Net win excl BE | Survivor TP | Required TP | Margin pp | "
    "Margin CI low | Margin CI high | Gross maxDD pips | Net maxDD pips | Gross maxDD R | "
    "Net maxDD R |"
)
_COST_HEADER = (
    "| # | Mode | ORB | Delay | MaxAge | Execution pips | Financing pips | Total cost pips | "
    "Break-even pips/side | Actual sides | Weighted sides | Entry fills | Exit fills | "
    "Cancelled | Expired | Median hold h | p95 hold h | Max concurrent | Suppressed | "
    "Unresolved | PropGuard | Breach events |"
)


def render_scale_sweep_markdown(report: ScaleSweepReport) -> str:
    lines: list[str] = []
    lines += _preamble(report)
    lines += _shared_configuration(report)
    lines += _core_table(report)
    lines += _cost_table(report)
    lines += _bucket_tables(report)
    lines += _descriptive_summary(report)
    return "\n".join(lines).rstrip() + "\n"


def _preamble(report: ScaleSweepReport) -> list[str]:
    coverage = report.m1_coverage
    return [
        "# S8 scale decomposition",
        "",
        "The complete §8.1 grid on one immutable candle set: "
        f"`{len(report.entry_modes)} entry modes x {len(report.orb_minutes_grid)} ORB values x "
        f"{len(report.entry_delay_minutes_grid)} entry delays x "
        f"{len(report.max_age_hours_grid)} max-age values = {report.expected_cell_count}` cells, "
        f"of which {len(report.cells)} are reported below.",
        "",
        "This is descriptive measurement of one local candle cache. It is **not** a "
        "strategy-selection result, no cell is recommended for production, no parameter was "
        "tuned, and losing cells are reported in full. A negative surface is a valid result.",
        "",
        "## Run identity",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol | {report.symbol} |",
        f"| Timeframe | {report.timeframe.value} |",
        f"| Source | {report.source} |",
        f"| Bars | {report.bar_count} |",
        f"| First bar (UTC) | {report.first_bar_ts.isoformat()} |",
        f"| Last bar (UTC) | {report.last_bar_ts.isoformat()} |",
        f"| Candle fingerprint (sha256) | `{report.candle_set_sha256}` |",
        f"| Sessions | {', '.join(report.sessions)} |",
        f"| Time exit mode | {report.shared_params.get('time_exit_mode')} (every cell) |",
        f"| Hold buckets | {', '.join(report.hold_bucket_labels)} |",
        "",
        "## M1 coverage and subpath fallback",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| `INTRABAR_MODE` | {coverage.intrabar_mode.value} |",
        f"| M1 coverage status | **{coverage.status}** |",
        f"| M1 bars loaded | {coverage.m1_bars_loaded} |",
        f"| M1 first bar | {_ts(coverage.m1_first_bar_ts)} |",
        f"| M1 last bar | {_ts(coverage.m1_last_bar_ts)} |",
        f"| Parent bars with covering M1 | {coverage.covered_parent_bars} / "
        f"{coverage.total_parent_bars} ({coverage.covered_parent_fraction:.2%}) |",
        f"| M1 subpath chronology used | {'yes' if coverage.subpath_used else 'no'} |",
        f"| Fallback | {coverage.subpath_fallback or 'none (M1 subpath used)'} |",
        "",
        coverage.fallback_description,
        "",
    ]


def _shared_configuration(report: ScaleSweepReport) -> list[str]:
    lines = [
        "## Shared configuration",
        "",
        "Identical in every cell. The four grid fields (`entry_mode`, `orb_minutes`, "
        "`entry_delay_minutes`, `max_age_hours`) are excluded here because they vary; every "
        "other field below was held fixed and each cell was validated, not copied unchecked.",
        "",
        "| Parameter | Value |",
        "|---|---|",
    ]
    for key in sorted(report.shared_params):
        lines.append(f"| `{key}` | `{report.shared_params[key]}` |")
    lines.append("")
    return lines


def _core_table(report: ScaleSweepReport) -> list[str]:
    lines = [
        "## All cells: paired gross and net performance",
        "",
        "Headline gross/net pips and R are final marked equity including unresolved "
        "structures; expectancy, profit factor, win rate and hold statistics use completed "
        "structures.",
        "",
        _CORE_HEADER,
        "|" + "---|" * 27,
    ]
    for cell in report.cells:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(cell.cell_index),
                    cell.entry_mode.value,
                    str(cell.orb_minutes),
                    str(cell.entry_delay_minutes),
                    _num(cell.max_age_hours, 0),
                    str(cell.completed_structures),
                    _num(cell.gross_pips, 2),
                    _num(cell.net_pips, 2),
                    _num(cell.gross_r, 4),
                    _num(cell.net_r, 4),
                    _num(cell.gross_expectancy_pips, 2),
                    _num(cell.net_expectancy_pips, 2),
                    _num(cell.gross_expectancy_r, 4),
                    _num(cell.net_expectancy_r, 4),
                    _num(cell.gross_profit_factor, 4),
                    _num(cell.net_profit_factor, 4),
                    _pct(cell.gross_win_rate_excl_be),
                    _pct(cell.net_win_rate_excl_be),
                    _pct(cell.survivor_tp_rate),
                    _pct(cell.breakeven_tp_rate_required),
                    _num(cell.tp_rate_margin_pp, 2),
                    _num(cell.tp_rate_margin_pp_ci_low, 2),
                    _num(cell.tp_rate_margin_pp_ci_high, 2),
                    _num(cell.gross_max_drawdown_pips, 2),
                    _num(cell.net_max_drawdown_pips, 2),
                    _num(cell.gross_max_drawdown_r, 4),
                    _num(cell.net_max_drawdown_r, 4),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def _cost_table(report: ScaleSweepReport) -> list[str]:
    lines = [
        "## All cells: cost, execution, concurrency and guard state",
        "",
        _COST_HEADER,
        "|" + "---|" * 22,
    ]
    for cell in report.cells:
        guard = "breached" if cell.prop_guard_breached else "clear"
        if cell.prop_guard_breach_reason:
            guard = f"{guard} ({cell.prop_guard_breach_reason})"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(cell.cell_index),
                    cell.entry_mode.value,
                    str(cell.orb_minutes),
                    str(cell.entry_delay_minutes),
                    _num(cell.max_age_hours, 0),
                    _num(cell.execution_cost_pips, 2),
                    _num(cell.financing_cost_pips, 2),
                    _num(cell.total_cost_pips, 2),
                    _num(cell.breakeven_pips_per_completed_side, 4),
                    str(cell.transaction_sides),
                    _num(cell.cost_side_equivalents, 2),
                    str(cell.entry_fill_sides),
                    str(cell.exit_fill_sides),
                    str(cell.cancelled_entry_orders),
                    str(cell.expired_entry_orders),
                    _num(cell.median_hold_hours, 2),
                    _num(cell.p95_hold_hours, 2),
                    str(cell.max_concurrent_structures),
                    str(cell.suppressed_signals),
                    str(cell.unresolved_structures),
                    guard,
                    str(cell.prop_guard_breach_events),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def _bucket_tables(report: ScaleSweepReport) -> list[str]:
    labels = [bucket.label for bucket in HOLD_BUCKETS]
    header = (
        "| # | Mode | ORB | Delay | MaxAge | "
        + " | ".join(f"n {label}" for label in labels)
        + " | "
        + " | ".join(f"gross R {label}" for label in labels)
        + " | "
        + " | ".join(f"net R {label}" for label in labels)
        + " | Unbucketed | Completed gross R | Completed net R |"
    )
    lines = [
        "## All cells: hold-bucket gross/net R attribution",
        "",
        "Buckets are fixed, non-overlapping and exhaustive over completed structures: "
        f"{', '.join(labels)}. `Unbucketed` counts completed structures with no recorded exit "
        "timestamp; bucket counts plus unbucketed equal the completed count, and the bucket R "
        "columns sum to the completed-structure totals in the last two columns.",
        "",
        header,
        "|" + "---|" * (5 + 3 * len(labels) + 3),
    ]
    for cell in report.cells:
        by_label = {bucket.label: bucket for bucket in cell.hold_buckets}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(cell.cell_index),
                    cell.entry_mode.value,
                    str(cell.orb_minutes),
                    str(cell.entry_delay_minutes),
                    _num(cell.max_age_hours, 0),
                    *[str(by_label[label].structures) for label in labels],
                    *[_num(by_label[label].gross_r, 4) for label in labels],
                    *[_num(by_label[label].net_r, 4) for label in labels],
                    str(cell.unbucketed_structures),
                    _num(cell.completed_gross_r, 4),
                    _num(cell.completed_net_r, 4),
                ]
            )
            + " |"
        )
    lines.append("")
    lines += _bucket_pips_table(report, labels)
    return lines


def _bucket_pips_table(report: ScaleSweepReport, labels: list[str]) -> list[str]:
    header = (
        "| # | Mode | ORB | Delay | MaxAge | "
        + " | ".join(f"gross pips {label}" for label in labels)
        + " | "
        + " | ".join(f"net pips {label}" for label in labels)
        + " | Completed gross pips | Completed net pips |"
    )
    lines = [
        "## All cells: hold-bucket gross/net pip attribution",
        "",
        header,
        "|" + "---|" * (5 + 2 * len(labels) + 2),
    ]
    for cell in report.cells:
        by_label = {bucket.label: bucket for bucket in cell.hold_buckets}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(cell.cell_index),
                    cell.entry_mode.value,
                    str(cell.orb_minutes),
                    str(cell.entry_delay_minutes),
                    _num(cell.max_age_hours, 0),
                    *[_num(by_label[label].gross_pips, 2) for label in labels],
                    *[_num(by_label[label].net_pips, 2) for label in labels],
                    _num(cell.completed_gross_pips, 2),
                    _num(cell.completed_net_pips, 2),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def _descriptive_summary(report: ScaleSweepReport) -> list[str]:
    cells = report.cells
    positive_net_r = [cell for cell in cells if cell.net_r > 0]
    gate_pass = [
        cell
        for cell in cells
        if cell.tp_rate_margin_pp_ci_low is not None and cell.tp_rate_margin_pp_ci_low > 0
    ]
    no_completed = [cell for cell in cells if cell.completed_structures == 0]
    breached = [cell for cell in cells if cell.prop_guard_breached]

    lines = [
        "## Descriptive read of the surface",
        "",
        "No cell below is selected, recommended, or promoted. These are counts and marginal "
        "distributions over the same 256 rows printed above.",
        "",
        f"- Cells with net R above zero: **{len(positive_net_r)} / {len(cells)}**.",
        f"- Cells whose TP-rate margin confidence interval excludes zero (§9 gate "
        f'"does the TP rate clear its bar"): **{len(gate_pass)} / {len(cells)}**.',
        f"- Cells with no completed structure on this candle set: **{len(no_completed)} / "
        f"{len(cells)}**.",
        f"- Cells where PropGuard breached: **{len(breached)} / {len(cells)}**.",
        "",
        "### Marginal net R by grid axis",
        "",
        "Median and mean net R across every cell holding that axis level fixed, with the count "
        "of cells above zero. A broad plateau, if one exists, shows up as several adjacent "
        "levels behaving alike; a single strong level with weak neighbours is noise until it "
        "survives out of sample.",
        "",
        "| Axis | Level | Cells | Median net R | Mean net R | Cells net R > 0 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    axes: list[tuple[str, list[tuple[str, list[ScaleSweepCell]]]]] = [
        (
            "entry_mode",
            [
                (mode.value, [cell for cell in cells if cell.entry_mode is mode])
                for mode in report.entry_modes
            ],
        ),
        (
            "orb_minutes",
            [
                (str(orb), [cell for cell in cells if cell.orb_minutes == orb])
                for orb in report.orb_minutes_grid
            ],
        ),
        (
            "entry_delay_minutes",
            [
                (str(delay), [cell for cell in cells if cell.entry_delay_minutes == delay])
                for delay in report.entry_delay_minutes_grid
            ],
        ),
        (
            "max_age_hours",
            [
                (_num(age, 0), [cell for cell in cells if cell.max_age_hours == age])
                for age in report.max_age_hours_grid
            ],
        ),
    ]
    for axis, levels in axes:
        for level, subset in levels:
            values = [cell.net_r for cell in subset]
            lines.append(
                f"| `{axis}` | {level} | {len(subset)} | "
                f"{_num(median(values) if values else None, 4)} | "
                f"{_num(sum(values) / len(values) if values else None, 4)} | "
                f"{sum(1 for value in values if value > 0)} |"
            )
    lines += _degeneracy_section(cells)
    lines += [
        "",
        "### Hold-bucket totals across the whole surface",
        "",
        "| Bucket | Structures | Gross R | Net R | Gross pips | Net pips |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bucket in HOLD_BUCKETS:
        rows = [item for cell in cells for item in cell.hold_buckets if item.label == bucket.label]
        lines.append(
            f"| {bucket.label} | {sum(row.structures for row in rows)} | "
            f"{_num(sum(row.gross_r for row in rows), 4)} | "
            f"{_num(sum(row.net_r for row in rows), 4)} | "
            f"{_num(sum(row.gross_pips for row in rows), 2)} | "
            f"{_num(sum(row.net_pips for row in rows), 2)} |"
        )
    lines += [
        "",
        "### Caveats that bound every number above",
        "",
        f"- One local candle cache of {report.bar_count} {report.timeframe.value} bars from "
        f"{report.first_bar_ts.isoformat()} to {report.last_bar_ts.isoformat()}. That window is "
        "long enough to exercise the harness and describe behaviour, and far too short to "
        "select a configuration.",
        "- Repeated grid evaluation on one window is exactly the setting in which an argmax is "
        "an artefact. No walk-forward, no deflated Sharpe, no PBO, and no Monte Carlo has been "
        "run here; §9 answers those questions, and S8 does not.",
        f"- {report.m1_coverage.fallback_description}",
        "- Costs are configured, not measured from broker ticks. Where execution and financing "
        "cost columns are zero, that is the configuration, not evidence of a free trade.",
        "- Gross and net are reported side by side everywhere because §0.7 showed they can "
        "disagree in sign.",
    ]
    return lines


def _degeneracy_section(cells: list[ScaleSweepCell]) -> list[str]:
    """The delay axis is not four independent levels; say so rather than implying it is."""
    groups: dict[tuple[object, ...], list[ScaleSweepCell]] = {}
    for cell in cells:
        key = (
            cell.entry_mode,
            cell.orb_minutes,
            cell.max_age_hours,
            max(cell.orb_minutes, cell.entry_delay_minutes),
        )
        groups.setdefault(key, []).append(cell)
    duplicated = sum(len(group) - 1 for group in groups.values())
    collapsed_groups = [group for group in groups.values() if len(group) > 1]
    measured_identical = sum(
        1
        for group in collapsed_groups
        if len({(cell.gross_r, cell.net_r, cell.completed_structures) for cell in group}) == 1
    )
    lines = [
        "",
        "### Structural degeneracy on the entry-delay axis",
        "",
        "The engine fills at `max(anchor + ORB_MINUTES, anchor + ENTRY_DELAY_MINUTES)`, so any "
        "delay at or below the opening range is absorbed by the range close. The delay axis "
        "therefore does not contribute four independent levels, and duplicate rows above are "
        "duplicates by construction rather than independent evidence.",
        "",
        f"- Distinct effective configurations: **{len(groups)}** of {len(cells)} cells.",
        f"- Cells identical to an earlier cell by construction: **{duplicated}**.",
        f"- Collapsed groups whose measured gross R, net R and completed count agree exactly: "
        f"**{measured_identical} / {len(collapsed_groups)}** (any disagreement would be a bug).",
        "",
        "| Effective entry offset (minutes) | Cells | Median net R | Cells net R > 0 |",
        "|---:|---:|---:|---:|",
    ]
    offsets = sorted({max(cell.orb_minutes, cell.entry_delay_minutes) for cell in cells})
    for offset in offsets:
        subset = [
            cell for cell in cells if max(cell.orb_minutes, cell.entry_delay_minutes) == offset
        ]
        values = [cell.net_r for cell in subset]
        lines.append(
            f"| {offset} | {len(subset)} | {_num(median(values) if values else None, 4)} | "
            f"{sum(1 for value in values if value > 0)} |"
        )
    return lines


def _num(value: float | None, digits: int) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _ts(value: datetime | None) -> str:
    return "—" if value is None else value.isoformat()
