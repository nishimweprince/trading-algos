from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import create_app
from config import Settings
from models import Timeframe

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        paper_enabled=False,
        api_key=None,
    )
    store_dir = settings.local_candles_path("XAUUSD", Timeframe.M15)
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    store_dir.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_live(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_backtest_local_fixture(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "timeframe": "M15", "source": "local"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "local"
    assert body["bar_count"] > 0
    assert body["performance_unit"] == "pips"
    assert isinstance(body["realized_pips"], float)
    assert isinstance(body["max_drawdown_pips"], float)
    assert body["gross_realized_pips"] == body["realized_pips"]
    assert body["net_realized_pips"] == body["gross_realized_pips"] - body["realized_cost_pips"]
    assert body["net_realized_r"] == body["gross_realized_r"] - body["realized_cost_r"]
    assert "breakeven_pips_per_side" in body
    assert "cost_headroom_ratio" in body
    assert body["risk_mode"] == "fixed_qty"
    assert "suppressed_signal_count" in body
    assert all("qty" in pair for pair in body["trade_pairs"])
    assert body["realized_dollars"] is None
    assert body["trade_pairs"]
    assert body["equity_curve"]
    assert len(body["candle_set_sha256"]) == 64
    assert body["effective_settings"]["survivor_exit_mode"] == "legacy_lock"
    assert body["effective_settings"]["hedge_path_mode"] == "legacy_parent_bar"
    assert len(body["equity_curve"]) == body["bar_count"]
    timestamps = [point["ts"] for point in body["equity_curve"]]
    assert timestamps == sorted(set(timestamps))
    assert body["equity_curve"][-1]["net_equity"] == pytest.approx(
        body["performance"]["net_equity"]
    )
    assert max(point["net_drawdown"] for point in body["equity_curve"]) == pytest.approx(
        body["performance"]["net_max_drawdown"]
    )
    assert "trades" in body  # Legacy flat closed-leg contract remains available.
    assert (
        body["long_wins"]
        + body["long_be"]
        + body["long_loss"]
        + body["short_wins"]
        + body["short_be"]
        + body["short_loss"]
        + 2 * body["open_pairs"]
        >= 0
    )
    assert "tokyo" in {event["session"] for event in body["events"] if event["kind"] == "entry"}


def test_backtest_risk_override(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "lock_pips": 5, "sl_mult": 2, "rr": 3},
    )
    assert response.status_code == 200
    assert response.json()["bar_count"] > 0


def test_backtest_synthetic_entry_mode_override(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "entry_mode": "synthetic_breakout",
            "time_exit_mode": "none",
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry_mode"] == "synthetic_breakout"
    assert body["transaction_sides"] <= 2 * len(body["trade_pairs"])
    assert any(event["kind"] == "entry_order_staged" for event in body["events"])


def test_backtest_contingent_override_is_revalidated(client: TestClient) -> None:
    rejected = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "entry_mode": "contingent_hedge",
            "hedge_ratio_initial": 1,
            "hedge_ratio_staged": 0.5,
        },
    )
    assert rejected.status_code == 422
    assert "HEDGE_RATIO_STAGED" in rejected.json()["detail"]


def test_backtest_oco_bracket_override(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "entry_mode": "oco_bracket",
            "oco_buffer_mode": "fixed_pips",
            "oco_buffer_value": 1,
            "oco_expiry_bars": 2,
            "allow_reentry": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry_mode"] == "oco_bracket"
    assert any(event["kind"] == "entry_order_staged" for event in body["events"])


def test_backtest_chronological_survivor_settings_are_additive(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "survivor_exit_mode": "mfe_trail",
            "survivor_trail_activation_r": 1.25,
            "survivor_trail_gap_r": 1.5,
            "hedge_path_mode": "chronological_v2",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report_header"]["survivor_exit_mode"] == "mfe_trail"
    assert body["report_header"]["survivor_trail_activation_r"] == 1.25
    assert body["report_header"]["survivor_trail_gap_r"] == 1.5
    assert body["report_header"]["hedge_path_mode"] == "chronological_v2"
    assert body["effective_settings"]["survivor_exit_mode"] == "mfe_trail"
    assert all("survivor_ratchet_advances" in pair for pair in body["trade_pairs"])


def test_backtest_rejects_sub_one_r_survivor_activation(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "survivor_exit_mode": "mfe_trail",
            "survivor_trail_activation_r": 0.75,
        },
    )
    assert response.status_code == 422
    assert "SURVIVOR_TRAIL_ACTIVATION_R" in response.json()["detail"]


def test_four_mode_comparison_endpoint_uses_one_candle_fingerprint(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/backtests/compare",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "spread_pips_per_side": 1,
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["entry_mode"] for row in body["rows"]] == [
        "hedge_pair",
        "synthetic_breakout",
        "contingent_hedge",
        "oco_bracket",
    ]
    assert len(body["candle_set_sha256"]) == 64
    assert abs(body["hedge_vs_synthetic"]["reconciliation_error_pips"]) < 1e-9


def test_backtest_fixed_stop_override_pins_every_stop(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "stop_mode": "fixed_pips",
            "fixed_stop_pips": 150,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stop_mode"] == "fixed_pips"
    assert body["fixed_stop_pips"] == 150.0
    entries = [event for event in body["events"] if event["kind"] == "entry"]
    assert entries
    assert {round(event["detail"]["sl_dist"], 6) for event in entries} == {15.0}


def test_backtest_fixed_stop_without_distance_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "stop_mode": "fixed_pips"},
    )
    assert response.status_code == 422
    assert "FIXED_STOP_PIPS" in response.json()["detail"]


def test_backtest_dollar_mode_uses_the_default_rate_when_none_is_sent(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "performance_unit": "dollars"},
    )
    assert response.status_code == 200
    performance = response.json()["performance"]
    assert performance["unit"] == "dollars"
    assert performance["dollars_per_pip_per_qty"] == 10.0
    assert performance["unit_label"] == "$"


def test_backtest_dollar_mode_honours_the_client_rate(client: TestClient) -> None:
    body = {"symbol": "XAUUSD", "source": "local"}
    pips = client.post("/v1/backtests", json=body).json()
    dollars = client.post(
        "/v1/backtests",
        json=body | {"performance_unit": "dollars", "dollars_per_pip_per_qty": 2.5},
    ).json()

    assert pips["performance"]["unit"] == "pips"
    assert pips["performance"]["conversion_factor"] == 1.0
    assert pips["performance"]["dollars_per_pip_per_qty"] is None
    assert dollars["performance"]["conversion_factor"] == 2.5
    # Same run, restated: every additive metric scales by the one factor.
    assert dollars["performance"]["net_equity"] == pytest.approx(
        pips["performance"]["net_equity"] * 2.5
    )
    assert dollars["performance"]["gross_max_drawdown"] == pytest.approx(
        pips["performance"]["gross_max_drawdown"] * 2.5
    )
    assert [point["ts"] for point in dollars["equity_curve"]] == [
        point["ts"] for point in pips["equity_curve"]
    ]
    assert [point["net_equity"] for point in dollars["equity_curve"]] == pytest.approx(
        [point["net_equity"] * 2.5 for point in pips["equity_curve"]]
    )
    assert [point["net_drawdown"] for point in dollars["equity_curve"]] == pytest.approx(
        [point["net_drawdown"] * 2.5 for point in pips["equity_curve"]]
    )
    # The pip series itself never moves, and R is a ratio, so neither is converted.
    assert dollars["gross_equity_pips"] == pips["gross_equity_pips"]
    assert dollars["gross_equity_r"] == pips["gross_equity_r"]


def test_backtest_cost_override_is_revalidated(client: TestClient) -> None:
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "swap_timezone": "Not/A_Zone"},
    )
    assert response.status_code == 422
    assert "SWAP_TIMEZONE" in response.json()["detail"]


def test_fixed_fractional_sizes_in_cash_even_when_results_are_shown_in_pips(
    client: TestClient,
) -> None:
    """Sizing needs a cash rate whatever unit the results are displayed in."""
    response = client.post(
        "/v1/backtests",
        json={"symbol": "XAUUSD", "source": "local", "risk_mode": "fixed_fractional"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["risk_mode"] == "fixed_fractional"
    assert body["performance"]["unit"] == "pips"
    assert body["performance"]["conversion_factor"] == 1.0
    assert body["performance"]["dollars_per_pip_per_qty"] == 10.0


def test_custom_firm_profile_gets_a_cash_rate_without_asking_for_dollars(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/backtests",
        json={
            "symbol": "XAUUSD",
            "source": "local",
            "firm_profile": "custom",
            "dollars_per_pip_per_qty": 4,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["firm_profile"] == "custom"
    assert body["performance"]["unit"] == "pips"
    assert body["performance"]["dollars_per_pip_per_qty"] == 4.0


def test_candles_local(client: TestClient) -> None:
    response = client.get("/v1/candles?symbol=XAUUSD&timeframe=M15&source=local&count=10")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "local"
    assert len(body["candles"]) == 10


def test_service_config(client: TestClient) -> None:
    response = client.get("/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "XAUUSD"
    assert body["timeframe"] == "M15"
    assert body["sessions"] == ["tokyo", "london", "new_york"]
    assert body["lock_pips"] == 20.0
    assert body["lock_mode"] == "absolute"
    assert body["lock_r"] == 0.0
    assert body["stop_mode"] == "bar_range"
    assert body["sl_mult"] == 2.0
    assert body["fixed_stop_pips"] == 0.0
    assert body["rr"] == 3.0
    assert body["tp_mode"] == "fixed_r"
    assert body["partial_tp_r"] == 1.0
    assert body["partial_fraction"] == 0.5
    assert body["survivor_exit_mode"] == "legacy_lock"
    assert body["survivor_trail_activation_r"] == 1.5
    assert body["survivor_trail_gap_r"] == 1.0
    assert body["hedge_path_mode"] == "legacy_parent_bar"
    assert body["min_stop_pips"] == 0.0
    assert body["min_stop_cost_mult"] == 0.0
    assert body["filter_d1_ema50"] is False
    assert body["filter_nr7"] is False
    assert body["filter_orb_atr_min"] == 0.0
    assert body["filter_orb_atr_max"] == 0.0
    assert body["qty"] == 1.0
    assert body["pip_size"] == 0.1
    assert body["point_value"] == 1.0
    assert body["orb_minutes"] == 60
    assert body["entry_delay_minutes"] == 15
    assert body["anchor_tolerance_minutes"] == 15
    assert body["intrabar_mode"] == "m1_conservative"
    assert "performance_unit" not in body
    assert body["default_dollars_per_pip_per_qty"] == 10.0
    assert body["cost_model"] == "per_session"
    assert body["spread_pips_per_side"] == 0.0
    assert body["swap_timezone"] == "America/New_York"
    assert body["breakeven_cost_report"] is True
    assert body["risk_mode"] == "fixed_qty"
    assert body["risk_pct_per_r"] == 0.1
    assert body["max_pair_risk_pct"] == 0.2
    assert body["max_open_risk_pct"] == 0.75
    assert body["max_concurrent_structures"] == 3
    assert body["one_open_per_session"] is True
    assert body["firm_profile"] == "none"
    assert body["firm_initial_balance"] == 100_000
    assert body["firm_daily_loss_limit_pct"] == 5.0
    assert body["firm_total_loss_limit_pct"] == 10.0
    assert body["time_exit_mode"] == "max_age"
    assert body["max_age_hours"] == 24.0
    assert "api_key" not in body
    assert "ctrader_api_key" not in body


def test_paper_status_when_disabled(client: TestClient) -> None:
    response = client.get("/v1/paper")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["prop_guard_breached"] is False


def test_s7_research_artifact_is_read_only_and_labelled_simulation(
    client: TestClient,
) -> None:
    response = client.get("/v1/research/s7-propguard-monte-carlo")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"]["kind"] == "research_simulation"
    assert body["source"]["not_interactive_backtest"] is True
    assert body["source"]["not_broker_fact"] is True
    assert body["study"] == "s7_propguard_monte_carlo"
    assert body["seed"] == 20260820
    assert len(body["modes"]) == 4
    hedge = next(row for row in body["modes"] if row["entry_mode"] == "hedge_pair")
    assert "worst_simulated_path_net_r" in hedge
    assert "daily_breach_days" in hedge
    assert "minimum_free_margin_pct_distribution" in hedge
    assert "headroom_path" in hedge
