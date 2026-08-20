from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path

import pytest

from costs import COST_IDENTITY_ABS_TOL
from engine import ClosedBarEngine, Pair
from fills import TickPathUnavailable, resolve_bar_levels, resolve_oco_trigger
from models import Candle, EngineParams, Timeframe
from sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


def _bar(*, open_: float, high: float, low: float, close: float, minute: int = 15) -> Candle:
    return Candle(
        ts=datetime(2026, 8, 18, 12, minute, tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1,
        provider="property",
        source_instrument="XAUUSD",
    )


@pytest.mark.parametrize("mode", ["optimistic", "pessimistic", "m1", "m1_conservative"])
@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize("open_", [90.0, 100.0, 110.0])
def test_stop_fills_are_never_better_than_the_configured_level(
    mode: str, is_long: bool, open_: float
) -> None:
    stop, tp = (100.0, 110.0) if is_long else (100.0, 90.0)
    bar = _bar(open_=open_, high=115, low=85, close=101)
    hit = resolve_bar_levels(
        mode=mode,
        is_long=is_long,
        bar=bar,
        stop=stop,
        tp=tp,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.fill is not None
    assert bar.low <= hit.fill <= bar.high
    if hit.kind == "stop":
        assert hit.fill <= stop if is_long else hit.fill >= stop


@pytest.mark.parametrize("mode", ["optimistic", "pessimistic", "m1", "m1_conservative"])
@pytest.mark.parametrize("bullish", [True, False])
@pytest.mark.parametrize("open_", [90.0, 100.0, 110.0])
def test_oco_stop_entry_fills_respect_trigger_and_ohlc(
    mode: str, bullish: bool, open_: float
) -> None:
    bar = _bar(open_=open_, high=112, low=88, close=101)
    hit = resolve_oco_trigger(
        mode=mode,
        bullish_signal=bullish,
        bar=bar,
        upper=105,
        lower=95,
        m1_bars=None,
        parent_minutes=15,
    )
    assert hit.fill is not None
    assert bar.low <= hit.fill <= bar.high
    assert hit.fill >= 105 if hit.side == "long" else hit.fill <= 95


def test_a_closed_structure_cannot_be_closed_twice_by_a_duplicate_bar() -> None:
    engine = ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            pip_size=1,
            stop_mode="fixed_pips",
            fixed_stop_pips=10,
            intrabar_mode="pessimistic",
            timeframe_minutes=15,
            orb_minutes=15,
            time_exit_mode="none",
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ),
    )
    engine.pairs.append(
        Pair(
            id="new_york:no-double-close",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=130,
            short_sl=110,
            short_tp=70,
            entry_ts=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        )
    )
    bar = _bar(open_=100, high=112, low=88, close=100)
    engine.step(bar)
    trade_count = len(engine.trades)
    engine.step(bar)
    assert trade_count == 2
    assert len(engine.trades) == trade_count


def test_configuration_matrix_smoke_and_cost_invariants() -> None:
    candles = [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    modes = ["hedge_pair", "synthetic_breakout", "contingent_hedge", "oco_bracket"]
    stops = ["bar_range", "fixed_pips"]
    intrabars = ["optimistic", "pessimistic", "m1", "m1_conservative"]
    combinations = list(product(modes, stops, ["fixed_r"], ["absolute"], intrabars))
    assert len(combinations) == 32
    for entry_mode, stop_mode, tp_mode, lock_mode, intrabar_mode in combinations:
        params = EngineParams(
            entry_mode=entry_mode,
            stop_mode=stop_mode,
            fixed_stop_pips=10,
            tp_mode=tp_mode,
            lock_mode=lock_mode,
            intrabar_mode=intrabar_mode,
            cost_model="per_session",
            spread_pips_per_side=0.4,
            slippage_pips_per_side=0.1,
            timeframe_minutes=15,
            orb_minutes=15,
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        )
        engine = ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)
        engine.run(candles)
        report = engine.report("XAUUSD", Timeframe.M15, "local")
        assert report.report_header.entry_mode.value == entry_mode
        assert report.report_header.tp_mode.value == tp_mode
        assert report.report_header.lock_mode.value == lock_mode
        assert report.net_equity_pips == pytest.approx(
            report.gross_equity_pips - report.equity_cost_pips, abs=COST_IDENTITY_ABS_TOL
        )
        assert report.net_equity_r == pytest.approx(
            report.gross_equity_r - report.equity_cost_r, abs=COST_IDENTITY_ABS_TOL
        )
        assert report.execution_cost_pips + report.financing_cost_pips == pytest.approx(
            report.equity_cost_pips, abs=COST_IDENTITY_ABS_TOL
        )
        assert COST_IDENTITY_ABS_TOL == 1e-9


def test_tick_configuration_is_an_explicit_unavailable_interface() -> None:
    with pytest.raises(TickPathUnavailable, match="tick source"):
        ClosedBarEngine(build_windows(["new_york"], {}), EngineParams(intrabar_mode="tick"))


def test_m1_fallback_counts_partial_coverage_at_resolver_call_sites() -> None:
    parent = _bar(open_=100, high=115, low=85, close=101)
    partial = [
        Candle(
            ts=parent.ts - timedelta(minutes=offset),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            provider="property",
            source_instrument="XAUUSD",
        )
        for offset in range(1, 4)
    ]
    engine = ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(intrabar_mode="m1_conservative", timeframe_minutes=15, orb_minutes=15),
        m1_bars=partial,
    )
    engine.pairs.append(
        Pair(
            id="new_york:partial-m1",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=130,
            short_sl=110,
            short_tp=70,
            entry_ts=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        )
    )
    engine.step(_bar(open_=100, high=112, low=95, close=101))
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.report_header.m1_bars_loaded == 3
    assert report.report_header.m1_resolver_calls >= 1
    assert report.report_header.m1_partial_coverage_count >= 1
    assert report.report_header.m1_fallback_count >= 1
    assert report.report_header.m1_covered_resolver_calls == 0


def test_warmup_bars_are_the_bars_consumed_by_elapsed_session_marking() -> None:
    stepped = ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(timeframe_minutes=15, orb_minutes=15),
    )
    stepped.step(_bar(open_=100, high=101, low=99, close=100))
    stepped_report = stepped.report("XAUUSD", Timeframe.M15, "local")
    assert stepped_report.report_header.warmup_bars == 0

    ran = ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(timeframe_minutes=15, orb_minutes=15),
    )
    ran.run([_bar(open_=100, high=101, low=99, close=100)])
    ran_report = ran.report("XAUUSD", Timeframe.M15, "local")
    assert ran_report.report_header.warmup_bars == 1


def test_report_labels_inclusive_and_ex_be_win_rates_and_holding_percentiles() -> None:
    engine = ClosedBarEngine(
        build_windows(["new_york"], {}),
        EngineParams(
            pip_size=1,
            stop_mode="fixed_pips",
            fixed_stop_pips=10,
            intrabar_mode="pessimistic",
            timeframe_minutes=15,
            orb_minutes=15,
            time_exit_mode="none",
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ),
    )
    engine.pairs.append(
        Pair(
            id="new_york:rates",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=130,
            short_sl=110,
            short_tp=70,
            entry_ts=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        )
    )
    engine.step(_bar(open_=100, high=112, low=88, close=100))
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.win_rate is not None
    assert report.win_rate_excl_be is not None
    assert report.win_rate <= report.win_rate_excl_be or report.long_be + report.short_be == 0
    assert report.median_hold_hours is not None
    assert report.p95_hold_hours is not None
    assert report.p95_hold_hours >= report.median_hold_hours
    assert report.firm_profile_name == "none"
    assert report.firm_profile_version is None
    assert report.report_header.firm_profile_name == "none"
