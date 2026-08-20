from __future__ import annotations

from pathlib import Path

import pytest

from comparison import COMPARISON_MODES, compare_entry_modes
from models import Candle, EngineParams, Timeframe
from sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_four_mode_comparison_uses_one_input_and_reports_required_metrics() -> None:
    candles = _candles()
    before = [candle.model_dump(mode="json") for candle in candles]
    report = compare_entry_modes(
        candles,
        build_windows(["tokyo", "london", "new_york"], {}),
        EngineParams(
            cost_model="per_session",
            spread_pips_per_side=1,
            time_exit_mode="max_age",
            max_age_hours=24,
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )

    assert [row.entry_mode for row in report.rows] == list(COMPARISON_MODES)
    assert report.bar_count == len(candles)
    assert len(report.candle_set_sha256) == 64
    assert "entry_mode" not in report.shared_params
    assert [candle.model_dump(mode="json") for candle in candles] == before
    for row in report.rows:
        assert row.gross_pips - row.total_cost_pips == pytest.approx(row.net_pips)
        assert row.gross_r >= row.net_r
        assert row.entry_fill_sides + row.exit_fill_sides == row.transaction_sides
        assert row.p95_hold_hours is None or row.p95_hold_hours >= 0
        assert row.median_hold_hours is None or row.median_hold_hours >= 0
        assert row.suppressed_signals >= 0
        assert row.unresolved_structures >= 0


def test_hedge_synthetic_attribution_reconciles_gross_cost_and_net() -> None:
    report = compare_entry_modes(
        _candles(),
        build_windows(["new_york"], {}),
        EngineParams(
            intrabar_mode="optimistic",
            cost_model="per_session",
            spread_pips_per_side=1,
            time_exit_mode="none",
            one_open_per_session=False,
            max_concurrent_structures=0,
            max_open_risk_pct=0,
        ),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )
    attribution = report.hedge_vs_synthetic
    hedge, synthetic = report.rows[:2]

    assert attribution.gross_difference_pips == pytest.approx(
        hedge.gross_pips - synthetic.gross_pips
    )
    assert attribution.net_difference_pips == pytest.approx(
        hedge.net_pips - synthetic.net_pips
    )
    assert attribution.reconciliation_error_pips == pytest.approx(0, abs=1e-9)
    assert attribution.reconciliation_error_r == pytest.approx(0, abs=1e-9)
    assert (
        attribution.gross_payoff_effect_pips
        + attribution.gap_effect_pips
        + attribution.same_bar_effect_pips
        - attribution.total_cost_difference_pips
        == pytest.approx(attribution.net_difference_pips)
    )
