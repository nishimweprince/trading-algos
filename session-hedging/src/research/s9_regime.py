"""S9: regime and trend attribution.

The H4 export showed 64% of winners long against 39% on H1, over a period when gold rose
sharply. A symmetric straddle in a trending instrument collects the drift, so an edge that
comes predominantly from one direction or one regime is a regime bet wearing a hedge
costume. This splits every mode's completed structures by calendar half, by trend regime,
and by session, and reports the long-versus-short split of surviving winners together with
where the net R actually came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from anchors import SessionAnchor
from cell_stats import candle_sha256, pair_cost_r, pair_gross_r, pair_outcome
from engine import ClosedBarEngine
from metrics import OutcomeKind, wilson_interval
from models import (
    Candle,
    EngineParams,
    EntryMode,
    S9DirectionalFlag,
    S9RegimeCell,
    S9RegimeReport,
    Timeframe,
    TradePairResult,
)
from research import markdown
from research.scale import m1_coverage
from sessions import SessionWindow

S9_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
S9_TREND_LOOKBACK_DAYS = 5
S9_TREND_DEADBAND_PIPS_PER_DAY = 50.0
S9_CONCENTRATION_THRESHOLD = 0.70


@dataclass(frozen=True, slots=True)
class _Structure:
    """One completed structure with the attributes S9 splits on."""

    entry_ts: datetime
    session: str
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    outcome: OutcomeKind
    winner_side: str | None
    half: str
    regime: str


def run_s9_regime_attribution(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
) -> S9RegimeReport:
    """Split every mode by calendar half, trend regime and session."""
    if not candles:
        raise ValueError("S9 requires at least one candle")

    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = m1_bars or [] if coverage.subpath_used else []
    midpoint = candles[0].ts + (candles[-1].ts - candles[0].ts) / 2
    regimes = trend_regimes(
        candles,
        lookback_days=S9_TREND_LOOKBACK_DAYS,
        deadband_pips_per_day=S9_TREND_DEADBAND_PIPS_PER_DAY,
        pip_size=params.pip_size,
    )

    cells: list[S9RegimeCell] = []
    flags: list[S9DirectionalFlag] = []
    for mode in S9_MODES:
        structures = _structures(
            candles,
            windows,
            params,
            anchors,
            mode=mode,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            m1_bars=subpath_bars,
            midpoint=midpoint,
            regimes=regimes,
        )
        for split_kind, split_key, subset in _splits(structures):
            cells.append(_cell(mode, split_kind, split_key, subset))
        flags.extend(_flags(mode, cells))

    return S9RegimeReport(
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        bar_count=len(candles),
        first_bar_ts=candles[0].ts,
        last_bar_ts=candles[-1].ts,
        candle_set_sha256=candle_sha256(candles),
        shared_params=_shared(params),
        entry_modes=list(S9_MODES),
        trend_lookback_days=S9_TREND_LOOKBACK_DAYS,
        trend_deadband_pips_per_day=S9_TREND_DEADBAND_PIPS_PER_DAY,
        calendar_split_ts=midpoint,
        price_first=candles[0].close,
        price_last=candles[-1].close,
        price_change_pips=(candles[-1].close - candles[0].close) / params.pip_size,
        trend_day_counts=_day_counts(regimes),
        concentration_threshold=S9_CONCENTRATION_THRESHOLD,
        m1_coverage=coverage,
        cells=cells,
        flags=flags,
    )


def trend_regimes(
    candles: list[Candle],
    *,
    lookback_days: int,
    deadband_pips_per_day: float,
    pip_size: float,
) -> dict[str, str]:
    """Label each UTC date `up`, `down` or `flat` by its trailing daily slope."""
    closes: dict[str, float] = {}
    for candle in candles:
        closes[candle.ts.date().isoformat()] = candle.close
    dates = sorted(closes)
    labels: dict[str, str] = {}
    for index, day in enumerate(dates):
        if index < lookback_days:
            labels[day] = "warmup"
            continue
        slope = (closes[day] - closes[dates[index - lookback_days]]) / pip_size / lookback_days
        if slope > deadband_pips_per_day:
            labels[day] = "up"
        elif slope < -deadband_pips_per_day:
            labels[day] = "down"
        else:
            labels[day] = "flat"
    return labels


def _day_counts(regimes: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in regimes.values():
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _structures(
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
    midpoint: datetime,
    regimes: dict[str, str],
) -> list[_Structure]:
    mode_params = EngineParams.model_validate(params.model_dump() | {"entry_mode": mode})
    engine = ClosedBarEngine(windows, mode_params, anchors, m1_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(
        update={"bar_count": len(candles)}
    )
    pairs = {pair.id: pair for pair in engine.pairs}
    structures: list[_Structure] = []
    for result in report.trade_pairs:
        if result.status != "closed":
            continue
        pair = pairs[result.id]
        gross_r = pair_gross_r(result, pair, mode_params)
        structures.append(
            _Structure(
                entry_ts=result.entry_ts,
                session=result.session,
                gross_pips=result.gross_pnl_pips or 0.0,
                net_pips=result.net_pnl_pips or 0.0,
                gross_r=gross_r,
                net_r=gross_r - pair_cost_r(result, pair, mode_params),
                outcome=pair_outcome(result, pair, mode_params),
                winner_side=_winner_side(result),
                half="first" if result.entry_ts <= midpoint else "second",
                regime=regimes.get(result.entry_ts.date().isoformat(), "warmup"),
            )
        )
    return structures


def _winner_side(result: TradePairResult) -> str | None:
    """The side of the leg that carried the structure, when one clearly did."""
    legs = [
        leg
        for leg in (result.primary, result.hedge, *result.unknown_legs)
        if leg is not None
    ]
    winners = [leg for leg in legs if leg.pnl_pips > 0]
    if len(winners) != 1:
        return None
    return winners[0].side


def _splits(structures: list[_Structure]) -> list[tuple[str, str, list[_Structure]]]:
    splits: list[tuple[str, str, list[_Structure]]] = [("all", "all", structures)]
    for half in ("first", "second"):
        splits.append(
            ("calendar_half", half, [item for item in structures if item.half == half])
        )
    for regime in ("up", "down", "flat", "warmup"):
        splits.append(
            ("trend_regime", regime, [item for item in structures if item.regime == regime])
        )
    for session in sorted({item.session for item in structures}):
        splits.append(
            ("session", session, [item for item in structures if item.session == session])
        )
    return splits


def _cell(
    mode: EntryMode, split_kind: str, split_key: str, subset: list[_Structure]
) -> S9RegimeCell:
    tp = [item for item in subset if item.outcome == "tp"]
    long_winners = sum(1 for item in tp if item.winner_side == "long")
    short_winners = sum(1 for item in tp if item.winner_side == "short")
    decided = long_winners + short_winners
    interval = wilson_interval(long_winners, decided)
    net_long = sum(item.net_r for item in subset if item.winner_side == "long")
    net_short = sum(item.net_r for item in subset if item.winner_side == "short")
    directional = abs(net_long) + abs(net_short)
    net_pips = [item.net_pips for item in subset]
    return S9RegimeCell(
        entry_mode=mode,
        split_kind=split_kind,
        split_key=split_key,
        completed_structures=len(subset),
        gross_pips=sum(item.gross_pips for item in subset),
        net_pips=sum(net_pips),
        gross_r=sum(item.gross_r for item in subset),
        net_r=sum(item.net_r for item in subset),
        gross_expectancy_r=(
            sum(item.gross_r for item in subset) / len(subset) if subset else None
        ),
        net_expectancy_r=sum(item.net_r for item in subset) / len(subset) if subset else None,
        gross_profit_factor=_profit_factor([item.gross_pips for item in subset]),
        net_profit_factor=_profit_factor(net_pips),
        win_rate_excl_be=_win_rate(net_pips),
        tp_structures=len(tp),
        long_winners=long_winners,
        short_winners=short_winners,
        long_winner_share=long_winners / decided if decided else None,
        long_winner_ci_low=interval[0] if interval else None,
        long_winner_ci_high=interval[1] if interval else None,
        net_r_from_long=net_long,
        net_r_from_short=net_short,
        long_net_r_share=abs(net_long) / directional if directional else None,
    )


def _flags(mode: EntryMode, cells: list[S9RegimeCell]) -> list[S9DirectionalFlag]:
    flags: list[S9DirectionalFlag] = []
    overall = next(
        (
            cell
            for cell in cells
            if cell.entry_mode is mode and cell.split_kind == "all"
        ),
        None,
    )
    if overall is None or overall.completed_structures == 0:
        return flags
    if (
        overall.long_winner_share is not None
        and overall.tp_structures > 0
        and (
            overall.long_winner_share >= S9_CONCENTRATION_THRESHOLD
            or overall.long_winner_share <= 1 - S9_CONCENTRATION_THRESHOLD
        )
    ):
        flags.append(
            S9DirectionalFlag(
                entry_mode=mode,
                reason="directional_winner_concentration",
                detail=(
                    f"{overall.long_winners} of {overall.long_winners + overall.short_winners} "
                    f"surviving winners were long "
                    f"({overall.long_winner_share:.1%}); the interval is "
                    f"[{overall.long_winner_ci_low:.1%}, {overall.long_winner_ci_high:.1%}]"
                ),
            )
        )
    halves = [
        cell
        for cell in cells
        if cell.entry_mode is mode and cell.split_kind == "calendar_half"
    ]
    total = sum(abs(cell.net_r) for cell in halves)
    for cell in halves:
        if total and abs(cell.net_r) / total >= S9_CONCENTRATION_THRESHOLD:
            flags.append(
                S9DirectionalFlag(
                    entry_mode=mode,
                    reason="calendar_half_concentration",
                    detail=(
                        f"the {cell.split_key} half carries {abs(cell.net_r) / total:.1%} of the "
                        f"absolute net R ({cell.net_r:+.4f}R of {overall.net_r:+.4f}R overall)"
                    ),
                )
            )
    regimes = [
        cell
        for cell in cells
        if cell.entry_mode is mode and cell.split_kind == "trend_regime"
    ]
    regime_total = sum(abs(cell.net_r) for cell in regimes)
    for cell in regimes:
        if regime_total and abs(cell.net_r) / regime_total >= S9_CONCENTRATION_THRESHOLD:
            flags.append(
                S9DirectionalFlag(
                    entry_mode=mode,
                    reason="trend_regime_concentration",
                    detail=(
                        f"the {cell.split_key} regime carries "
                        f"{abs(cell.net_r) / regime_total:.1%} of the absolute net R "
                        f"({cell.net_r:+.4f}R over {cell.completed_structures} structures)"
                    ),
                )
            )
    return flags


def _shared(params: EngineParams) -> dict[str, object]:
    shared = params.model_dump(mode="json")
    shared.pop("entry_mode", None)
    return shared


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses else None


def _win_rate(values: list[float]) -> float | None:
    directional = [value for value in values if abs(value) > 1e-12]
    if not directional:
        return None
    return sum(value > 0 for value in directional) / len(directional)


def render_s9_markdown(report: S9RegimeReport) -> str:
    """Every split for every mode, with the directional flags stated plainly."""
    direction = "rose" if report.price_change_pips > 0 else "fell"
    lines = [
        "# S9 regime and trend attribution",
        "",
        "A symmetric straddle in a trending instrument collects the drift. That is a real "
        "effect and a regime-dependent one, so any configuration whose edge comes "
        "predominantly from one direction or one regime is flagged here rather than "
        "presented as a strategy result.",
        "",
        f"Over this window gold {direction} from {report.price_first:.2f} to "
        f"{report.price_last:.2f}, a move of {report.price_change_pips:+.1f} pips. Trend "
        "regime is the trailing "
        f"{report.trend_lookback_days}-day slope of the daily close, labelled `up` or `down` "
        f"beyond ±{report.trend_deadband_pips_per_day:g} pips per day and `flat` inside that "
        "deadband; the first "
        f"{report.trend_lookback_days} days are `warmup` and are reported, not dropped.",
        "",
    ]
    lines += markdown.identity_section(
        report,
        extra=[
            ("Entry modes", ", ".join(mode.value for mode in report.entry_modes)),
            ("Calendar split (UTC)", report.calendar_split_ts.isoformat()),
            (
                "Trend day counts",
                ", ".join(f"{label} {count}" for label, count in report.trend_day_counts.items()),
            ),
            ("Concentration threshold", f"{report.concentration_threshold:.0%}"),
        ],
    )
    lines += markdown.m1_section(report.m1_coverage)
    lines += _s9_flag_section(report)
    rows = [
        [
            cell.entry_mode.value,
            cell.split_kind,
            cell.split_key,
            str(cell.completed_structures),
            markdown.num(cell.gross_pips),
            markdown.num(cell.net_pips),
            markdown.num(cell.gross_r, 4),
            markdown.num(cell.net_r, 4),
            markdown.num(cell.gross_expectancy_r, 4),
            markdown.num(cell.net_expectancy_r, 4),
            markdown.num(cell.gross_profit_factor, 4),
            markdown.num(cell.net_profit_factor, 4),
            markdown.pct(cell.win_rate_excl_be),
            str(cell.tp_structures),
            str(cell.long_winners),
            str(cell.short_winners),
            markdown.pct(cell.long_winner_share),
            markdown.pct(cell.long_winner_ci_low),
            markdown.pct(cell.long_winner_ci_high),
            markdown.num(cell.net_r_from_long, 4),
            markdown.num(cell.net_r_from_short, 4),
            markdown.pct(cell.long_net_r_share),
        ]
        for cell in report.cells
    ]
    lines += ["## Every mode and split", ""]
    lines += markdown.table(
        [
            "Mode", "Split", "Key", "Completed", "Gross pips", "Net pips", "Gross R", "Net R",
            "Gross exp R", "Net exp R", "Gross PF", "Net PF", "Win excl BE", "TP",
            "Long winners", "Short winners", "Long winner share", "CI low", "CI high",
            "Net R from long", "Net R from short", "Long share of |net R|",
        ],
        rows,
        align_right_from=3,
    )
    lines += _s9_caveats(report)
    return "\n".join(lines).rstrip() + "\n"


def _s9_flag_section(report: S9RegimeReport) -> list[str]:
    lines = [
        "## Directional and regime flags",
        "",
        f"A flag fires when at least {report.concentration_threshold:.0%} of the surviving "
        "winners fall on one side, or when one calendar half or one trend regime carries that "
        "share of the absolute net R. A flag is not a verdict; it marks a result that cannot "
        "be read as direction-neutral.",
        "",
    ]
    if not report.flags:
        return lines + ["No configuration tripped a flag on this candle set.", ""]
    return lines + markdown.table(
        ["Mode", "Reason", "Detail"],
        [[flag.entry_mode.value, flag.reason, flag.detail] for flag in report.flags],
        align_right_from=3,
    )


def _s9_caveats(report: S9RegimeReport) -> list[str]:
    return [
        "## Caveats",
        "",
        f"- {report.bar_count} {report.timeframe.value} bars covering "
        f"{sum(report.trend_day_counts.values())} calendar days, of which "
        f"{report.trend_day_counts.get('down', 0)} were labelled `down`. A window with almost "
        "no down-trend cannot establish that a strategy works in one, and the `down` rows here "
        "hold a handful of structures each.",
        "- Surviving-winner counts are small. The long-share intervals are wide enough to "
        "include an even split in most rows; the flag marks concentration, it does not "
        "establish it.",
        f"- {report.m1_coverage.fallback_description}",
        "- `Net R from long` and `Net R from short` attribute a structure to the side of its "
        "single winning leg. Structures with no winning leg, or with two, are counted in the "
        "totals but in neither directional column, so the two columns need not sum to net R.",
        "- Calendar halves split the window at its midpoint by time, not by structure count.",
    ]
