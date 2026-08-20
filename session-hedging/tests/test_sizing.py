from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine import ClosedBarEngine, Pair
from models import Candle, EngineParams, Timeframe
from sessions import build_windows
from sizing import fixed_fractional_size

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
H1_EXPORT = Path(__file__).parent / "fixtures" / "session-hedging-XAUUSD-H1.csv"


def _engine(**overrides: object) -> ClosedBarEngine:
    params = EngineParams.model_validate(
        EngineParams(
            orb_minutes=15,
            timeframe_minutes=15,
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ).model_dump()
        | overrides
    )
    return ClosedBarEngine(build_windows(["new_york"], {}), params)


def test_fixed_fractional_denominator_includes_both_slippage_sides() -> None:
    no_slippage = fixed_fractional_size(
        equity_cash=100_000,
        s_pips=100,
        slippage_pips_per_side=0,
        dollars_per_pip_per_qty=10,
        risk_pct_per_r=0.10,
        max_pair_risk_pct=0.20,
    )
    with_slippage = fixed_fractional_size(
        equity_cash=100_000,
        s_pips=100,
        slippage_pips_per_side=5,
        dollars_per_pip_per_qty=10,
        risk_pct_per_r=0.10,
        max_pair_risk_pct=0.20,
    )
    assert no_slippage.qty == pytest.approx(0.1)
    assert with_slippage.denominator_pips == pytest.approx(110.0)
    assert with_slippage.qty == pytest.approx(100 / 1100)
    assert with_slippage.qty < no_slippage.qty


def test_max_open_risk_blocks_new_pair_without_shrinking_existing_pair() -> None:
    engine = _engine(
        risk_mode="fixed_fractional",
        dollars_per_pip_per_qty=10,
        stop_mode="fixed_pips",
        fixed_stop_pips=100,
        max_open_risk_pct=0.30,
        max_concurrent_structures=10,
    )
    first_ts = datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    assert engine._open_pair("new_york", 2000, 1, first_ts, True) is True
    first_qty = engine.pairs[0].qty
    assert first_qty == pytest.approx(0.1)
    assert engine.pairs[0].initial_risk_pct == pytest.approx(0.2)

    assert (
        engine._open_pair("new_york", 2000, 1, first_ts + timedelta(days=1), True)
        is False
    )
    assert len(engine.pairs) == 1
    assert engine.pairs[0].qty == first_qty
    assert engine.suppressed_signal_reasons == {"max_open_risk_pct": 1}


def test_one_open_per_session_and_global_concurrency_suppress_and_report() -> None:
    one = _engine(one_open_per_session=True, max_concurrent_structures=3)
    ts = datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    assert one._open_pair("new_york", 2000, 1, ts, True) is True
    assert one._open_pair("new_york", 2000, 1, ts + timedelta(days=1), True) is False
    assert one.suppressed_signal_reasons == {"one_open_per_session": 1}

    capped = _engine(one_open_per_session=False, max_concurrent_structures=3)
    for offset in range(4):
        capped._open_pair("new_york", 2000, 1, ts + timedelta(days=offset), True)
    assert len(capped.pairs) == 3
    assert capped.suppressed_signal_reasons == {"max_concurrent_structures": 1}
    capped._concurrent_samples.append(3)
    report = capped.report("XAUUSD", Timeframe.M15, "local")
    assert report.max_concurrent_structures == 3
    assert report.suppressed_signal_count == 1
    assert report.suppressed_signal_reasons == {"max_concurrent_structures": 1}


def test_pips_weighted_is_additive_for_pair_specific_quantity() -> None:
    engine = _engine(qty_ref=1.0)
    ts = datetime(2026, 1, 14, 13, 15, tzinfo=UTC)
    pairs = [
        Pair(
            id="new_york:large",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=130,
            short_sl=110,
            short_tp=70,
            qty=2.0,
            short_open=False,
            entry_ts=ts,
        ),
        Pair(
            id="new_york:small",
            session="new_york",
            entry=100,
            sl_dist=10,
            long_sl=90,
            long_tp=130,
            short_sl=110,
            short_tp=70,
            qty=0.5,
            short_open=False,
            entry_ts=ts,
        ),
    ]
    engine.pairs.extend(pairs)
    engine._close_long(pairs[0], 101, ts + timedelta(minutes=15))  # +10 raw × 2
    engine._close_long(pairs[1], 99, ts + timedelta(minutes=15))  # −10 raw × 0.5
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.gross_realized_pips == pytest.approx(15.0)
    assert report.realized_pips == pytest.approx(15.0)
    assert engine.stats.realized_pips == pytest.approx(15.0)


def test_fixed_qty_parity_matches_phase_zero_fixture_cell() -> None:
    bars = [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    params = EngineParams(
        risk_mode="fixed_qty",
        qty=1,
        qty_ref=1,
        timeframe_minutes=15,
        orb_minutes=60,
        entry_delay_minutes=15,
        anchor_tolerance_minutes=15,
        intrabar_mode="m1_conservative",
        one_open_per_session=False,
        max_concurrent_structures=0,
        max_open_risk_pct=0,
    )
    engine = ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)
    engine.run(bars)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert len(engine.pairs) == 2
    assert len(engine.trades) == 3
    assert report.realized_pips == pytest.approx(42.0)
    assert report.unrealized_pips == pytest.approx(60.0)
    assert report.realized_r == pytest.approx(1.0)
    assert report.unrealized_r == pytest.approx(1.4285714286)
    assert report.locks == 2


@pytest.mark.skipif(
    not H1_EXPORT.is_file(),
    reason="W1.2 H1 acceptance fixture is absent from tests/fixtures",
)
def test_h1_export_concurrency_counterfactual_caps_at_three() -> None:
    rows: list[tuple[datetime, datetime, str]] = []
    with H1_EXPORT.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entry = datetime.fromisoformat(row["entry_time"].replace("Z", "+00:00"))
            exits = [
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in (row["primary_exit_time"], row["hedge_exit_time"])
                if value
            ]
            if exits:
                rows.append((entry, max(exits), row["session"]))
    active: list[tuple[datetime, str]] = []
    active_all: list[datetime] = []
    suppressed = 0
    observed_max = 0
    baseline_max = 0
    for entry, exit_ts, session in sorted(rows):
        active_all = [active_exit for active_exit in active_all if active_exit > entry]
        active_all.append(exit_ts)
        baseline_max = max(baseline_max, len(active_all))
        active = [item for item in active if item[0] > entry]
        if len(active) >= 3 or any(item[1] == session for item in active):
            suppressed += 1
            continue
        active.append((exit_ts, session))
        observed_max = max(observed_max, len(active))
    assert baseline_max == 10
    assert observed_max <= 3
    assert suppressed > 0
