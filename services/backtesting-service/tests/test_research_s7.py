from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backtesting_service.main import parse_args, run
from backtesting_service.models import Candle, EngineParams, Timeframe
from backtesting_service.research.s7_prop_monte_carlo import (
    S7_MODES,
    build_trade_clusters,
    render_s7_markdown,
    run_s7_prop_monte_carlo,
)
from backtesting_service.sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params() -> EngineParams:
    return EngineParams.model_validate(
        {
            "orb_minutes": 60,
            "entry_delay_minutes": 15,
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
        }
    )


def _report():
    return run_s7_prop_monte_carlo(
        _candles(),
        build_windows(["tokyo", "london", "new_york"], {}),
        _params(),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
        simulations=40,
        horizon_days=20,
    )


@pytest.fixture(scope="module")
def report():
    return _report()


def test_overlap_and_regime_blocks_preserve_complete_structures() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "structure_id": "london",
            "session": "london",
            "entry_ts": start,
            "exit_ts": start + timedelta(hours=8),
            "volatility_regime": "mid",
        },
        {
            "structure_id": "new_york",
            "session": "new_york",
            "entry_ts": start + timedelta(hours=6),
            "exit_ts": start + timedelta(hours=12),
            "volatility_regime": "mid",
        },
        {
            "structure_id": "next_mid",
            "session": "tokyo",
            "entry_ts": start + timedelta(days=1),
            "exit_ts": start + timedelta(days=1, hours=2),
            "volatility_regime": "mid",
        },
        {
            "structure_id": "high",
            "session": "london",
            "entry_ts": start + timedelta(days=2),
            "exit_ts": start + timedelta(days=2, hours=2),
            "volatility_regime": "high",
        },
    ]
    for row in rows:
        row.update(
            {
                "gross_pips": 1.0,
                "net_pips": 1.0,
                "gross_r": 0.1,
                "net_r": 0.1,
                "stop_pips": 10.0,
                "transaction_sides": 2,
                "entry_gap": False,
                "exit_gap": False,
            }
        )
    clusters = build_trade_clusters(rows)
    assert len(clusters) == 2
    assert clusters[0]["structure_ids"] == ["london", "new_york", "next_mid"]
    assert clusters[0]["max_concurrent_structures"] == 2
    assert clusters[1]["structure_ids"] == ["high"]


def test_all_incumbent_modes_and_complete_clusters_are_reported(report) -> None:
    assert [mode["entry_mode"] for mode in report["modes"]] == [mode.value for mode in S7_MODES]
    assert report["resampling"]["unit"] == "complete_trade_cluster"
    assert report["resampling"]["individual_legs_resampled"] is False
    for mode in report["modes"]:
        ids = [item for cluster in mode["clusters"] for item in cluster["structure_ids"]]
        assert len(ids) == mode["complete_structure_count"]
        assert len(ids) == len(set(ids))


def test_seed_reproducibility_and_full_output_contract(report) -> None:
    again = _report()
    assert json.dumps(again, sort_keys=True) == json.dumps(report, sort_keys=True)
    assert report["seed"] == 20260820
    for mode in report["modes"]:
        simulation = mode["simulation"]
        assert simulation["simulation_count"] == 40
        assert set(simulation["daily_limit_breaches"]) == {"3", "5"}
        assert set(simulation["total_limit_breaches"]) == {"6", "10"}
        assert "expected_time_to_target_days_conditional" in simulation
        assert "minimum_free_margin_pct_distribution" in simulation
        assert "path_gross_pips_distribution" in simulation
        assert "path_net_pips_distribution" in simulation
        assert "path_gross_r_distribution" in simulation
        assert "path_net_r_distribution" in simulation


def test_tail_models_and_concurrent_exposure_are_active(report) -> None:
    assert report["tail_model"]["spread_pips_per_side"]["median"] == 2.0
    assert report["tail_model"]["slippage_pips_per_side"]["mean"] == 0.5
    assert "risk-budget proxy" in report["firm_model"]["minimum_free_margin_definition"]
    for mode in report["modes"]:
        simulation = mode["simulation"]
        assert simulation["spread_cost_pips_distribution"]["mean"] > 0
        assert simulation["slippage_cost_pips_distribution"]["mean"] > 0
        assert simulation["max_concurrent_structures_distribution"]["max"] >= 1


def test_m1_fallback_data_limits_and_markdown(report) -> None:
    assert report["m1_coverage"]["status"] == "absent"
    assert report["m1_coverage"]["subpath_used"] is False
    assert report["data_sufficiency"]["prop_survivability_claim_supported"] is False
    markdown = render_s7_markdown(report)
    assert "seed" in markdown.lower()
    assert "Every empirical trade cluster" in markdown
    assert "not broker margin" in markdown


def test_cli_exposes_command_and_rejects_non_frozen_bar_count(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "data" / "candles" / "XAUUSD"
    target.mkdir(parents=True)
    (target / "M15.jsonl").write_bytes(FIXTURE.read_bytes())
    (tmp_path / ".env.s7test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert parse_args(["--run-s7-prop-monte-carlo"]).run_s7_prop_monte_carlo is True
    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s7test", "--run-s7-prop-monte-carlo", "--output-dir", "out"])
    assert exit_info.value.code == 1
