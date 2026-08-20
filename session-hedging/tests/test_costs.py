from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from costs import (
    COST_IDENTITY_ABS_TOL,
    CostSchedule,
    breakeven_cost_per_side,
    headroom_ratio,
    leg_cost,
    rollover_units,
    schedule_for,
)
from engine import ClosedBarEngine, Pair
from models import EngineParams, Timeframe
from sessions import build_windows
from units import cash

FIXTURES = Path(__file__).parent / "fixtures"
M15_EXPORT = FIXTURES / "session-hedging-XAUUSD-M15.csv"
H1_EXPORT = FIXTURES / "session-hedging-XAUUSD-H1.csv"


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


def test_swap_accrues_by_rollover_and_wednesday_is_triple() -> None:
    entry = _ts(2026, 1, 14, 16)  # Wednesday, before rollover.
    after_wednesday = _ts(2026, 1, 14, 18)
    after_thursday = _ts(2026, 1, 15, 18)
    assert (
        rollover_units(
            entry,
            after_wednesday,
            timezone="America/New_York",
            rollover_time="17:00",
            triple_weekday="wednesday",
        )
        == 3
    )
    assert (
        rollover_units(
            entry,
            after_thursday,
            timezone="America/New_York",
            rollover_time="17:00",
            triple_weekday="wednesday",
        )
        == 4
    )


def test_weekend_has_no_phantom_saturday_or_sunday_rollover() -> None:
    # Friday and Monday rollovers are each one unit; Saturday/Sunday are covered by Wednesday.
    assert (
        rollover_units(
            _ts(2026, 1, 16, 16),
            _ts(2026, 1, 19, 18),
            timezone="America/New_York",
            rollover_time="17:00",
            triple_weekday="wednesday",
        )
        == 2
    )


def test_leg_cost_charges_actual_sides_and_holding_duration() -> None:
    schedule = CostSchedule(
        spread_pips_per_side=2,
        slippage_pips_per_side=0.5,
        commission_pips_per_side=0.25,
        swap_long_pips_per_rollover=1.5,
    )
    cost = leg_cost(
        schedule=schedule,
        entry_ts=_ts(2026, 1, 15, 16),
        as_of=_ts(2026, 1, 16, 18),
        is_long=True,
        exited=True,
        timezone="America/New_York",
        rollover_time="17:00",
        triple_weekday="wednesday",
    )
    assert cost.execution_pips == pytest.approx(5.5)  # entry + exit
    assert cost.financing_pips == pytest.approx(3.0)  # Thursday + Friday
    assert cost.total_pips == pytest.approx(8.5)


def test_per_session_override_replaces_only_named_values() -> None:
    base = CostSchedule(spread_pips_per_side=2, commission_pips_per_side=0.25)
    london = schedule_for(
        session="london",
        enabled=True,
        base=base,
        overrides={"london": {"spread_pips_per_side": 3.5}},
    )
    assert london.spread_pips_per_side == pytest.approx(3.5)
    assert london.commission_pips_per_side == pytest.approx(0.25)
    assert (
        schedule_for(
            session="london", enabled=False, base=base, overrides={}
        ).execution_pips_per_side
        == 0.0
    )


def test_report_keeps_gross_cost_and_net_pips_and_r_together() -> None:
    params = EngineParams(
        orb_minutes=15,
        timeframe_minutes=15,
        spread_pips_per_side=1.0,
        slippage_pips_per_side=0.5,
        commission_pips_per_side=0.25,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    pair = Pair(
        id="new_york:costs",
        session="new_york",
        entry=2000,
        sl_dist=10,
        long_sl=1990,
        long_tp=2030,
        short_sl=2010,
        short_tp=1970,
        primary_side="long",
        entry_ts=_ts(2026, 1, 15, 10),
    )
    engine.pairs.append(pair)
    exit_ts = _ts(2026, 1, 15, 11)
    engine._close_long(pair, 2010, exit_ts)
    engine._close_short(pair, 1990, exit_ts)

    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.realized_pips == pytest.approx(200.0)  # compatibility alias remains gross
    assert report.gross_realized_pips == pytest.approx(200.0)
    assert report.execution_cost_pips == pytest.approx(7.0)  # 4 × 1.75
    assert report.financing_cost_pips == 0.0
    assert report.realized_cost_pips == pytest.approx(7.0)
    assert report.net_realized_pips == pytest.approx(193.0)
    assert report.gross_realized_r == pytest.approx(2.0)
    assert report.realized_cost_r == pytest.approx(0.07)
    assert report.net_realized_r == pytest.approx(1.93)
    assert report.transaction_sides == 4
    assert report.breakeven_pips_per_side == pytest.approx(50.0)
    assert report.cost_headroom_ratio == pytest.approx(50.0)


def test_zero_cost_configuration_reproduces_gross_exactly() -> None:
    params = EngineParams(
        orb_minutes=15,
        timeframe_minutes=15,
        cost_model="none",
        spread_pips_per_side=2.0,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    pair = Pair(
        id="new_york:zero",
        session="new_york",
        entry=2000,
        sl_dist=10,
        long_sl=1990,
        long_tp=2030,
        short_sl=2010,
        short_tp=1970,
        short_open=False,
        entry_ts=_ts(2026, 1, 15, 10),
    )
    engine.pairs.append(pair)
    engine._close_long(pair, 1990, _ts(2026, 1, 15, 11))
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.realized_cost_pips == 0.0
    assert report.net_realized_pips == report.gross_realized_pips == report.realized_pips
    assert report.net_realized_r == report.gross_realized_r == report.realized_r
    assert report.configured_spread_pips_per_side == 0.0
    assert report.cost_headroom_ratio is None


def test_execution_costs_move_net_drawdown_in_pips_and_r() -> None:
    params = EngineParams(
        orb_minutes=15,
        timeframe_minutes=15,
        spread_pips_per_side=2.0,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), params)
    entry_ts = _ts(2026, 1, 15, 10)
    engine.pairs.append(
        Pair(
            id="new_york:drawdown",
            session="new_york",
            entry=2000,
            sl_dist=10,
            long_sl=1990,
            long_tp=2030,
            short_sl=2010,
            short_tp=1970,
            entry_ts=entry_ts,
        )
    )
    engine._mark_equity(2000, entry_ts)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.gross_max_drawdown_pips == 0.0
    assert report.net_max_drawdown_pips == pytest.approx(4.0)
    assert report.gross_max_drawdown_r == 0.0
    assert report.net_max_drawdown_r == pytest.approx(0.04)


def test_cost_pips_and_cash_use_the_same_additive_amount() -> None:
    weighted_cost_pips = 7.0
    assert cash(weighted_cost_pips, dollars_per_pip_per_qty=2.0, qty_ref=1.0) == pytest.approx(14.0)


def test_cost_identity_tolerance_is_explicit_and_not_pytest_approx_default() -> None:
    assert COST_IDENTITY_ABS_TOL == 1e-9
    residual = 5e-10
    assert abs(residual) <= COST_IDENTITY_ABS_TOL
    assert abs(1e-8) > COST_IDENTITY_ABS_TOL


def test_break_even_budget_and_headroom_math() -> None:
    assert breakeven_cost_per_side(18.8, 4) == pytest.approx(4.7)
    assert breakeven_cost_per_side(18.8, 2) == pytest.approx(9.4)
    assert breakeven_cost_per_side(-1, 4) < 0  # type: ignore[operator]
    assert headroom_ratio(4.7, 2.0) == pytest.approx(2.35)
    assert headroom_ratio(4.7, 0.0) is None


@pytest.mark.skipif(
    not (M15_EXPORT.is_file() and H1_EXPORT.is_file()),
    reason="W1.1 acceptance fixtures are absent from tests/fixtures",
)
def test_export_break_even_cost_budget_matches_v3_reference() -> None:
    m15_gross, m15_pairs = _closed_export_gross(M15_EXPORT)
    h1_gross, h1_pairs = _closed_export_gross(H1_EXPORT)
    assert breakeven_cost_per_side(m15_gross, 4 * m15_pairs) <= 0  # type: ignore[operator]
    h1_four_sides = breakeven_cost_per_side(h1_gross, 4 * h1_pairs)
    h1_two_sides = breakeven_cost_per_side(h1_gross, 2 * h1_pairs)
    assert h1_four_sides == pytest.approx(4.7, abs=0.2)
    assert h1_two_sides == pytest.approx(9.4, abs=0.4)


def _closed_export_gross(path: Path) -> tuple[float, int]:
    gross = 0.0
    count = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["pair_status"] != "closed":
                continue
            gross += float(row["pair_pnl_pips"])
            count += 1
    return gross, count
