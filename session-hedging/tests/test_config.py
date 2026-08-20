from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import Settings


def test_rejects_unfilled_ctrader_api_key_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(ctrader_api_key="replace-with-ctrader-markets-api-key")


def test_blank_optional_secrets_are_none() -> None:
    settings = Settings(api_key="", notification_api_key="  ")
    assert settings.api_key is None
    assert settings.notification_api_key is None


def test_the_environment_no_longer_chooses_the_reporting_unit() -> None:
    """Pips versus dollars is a client choice; PERFORMANCE_UNIT in .env is inert."""
    params = Settings(performance_unit="dollars").engine_params()

    assert params.performance_unit == "pips"
    assert params.dollars_per_pip_per_qty is None
    assert not hasattr(Settings(), "performance_unit")


def test_fixed_stop_mode_requires_a_distance() -> None:
    with pytest.raises(ValidationError, match="FIXED_STOP_PIPS"):
        Settings(stop_mode="fixed_pips")


def test_fixed_stop_pips_reaches_engine_params() -> None:
    params = Settings(stop_mode="fixed_pips", fixed_stop_pips=150).engine_params()
    assert params.stop_mode == "fixed_pips"
    assert params.fixed_stop_pips == pytest.approx(150.0)


def test_stop_mode_defaults_to_bar_range() -> None:
    assert Settings().engine_params().stop_mode == "bar_range"


def test_entry_mode_defaults_and_synthetic_override_reach_engine() -> None:
    assert Settings().engine_params().entry_mode == "hedge_pair"
    assert Settings(entry_mode="synthetic_breakout").engine_params().entry_mode == (
        "synthetic_breakout"
    )


def test_contingent_hedge_surface_validates_and_reaches_engine() -> None:
    params = Settings(
        entry_mode="contingent_hedge",
        hedge_ratio_initial=0.5,
        hedge_ratio_staged=1.0,
        hedge_failure_k=0.25,
    ).engine_params()
    assert params.hedge_ratio_initial == pytest.approx(0.5)
    assert params.hedge_ratio_staged == pytest.approx(1.0)
    assert params.hedge_failure_k == pytest.approx(0.25)
    with pytest.raises(ValidationError, match="HEDGE_RATIO_STAGED"):
        Settings(hedge_ratio_initial=1.0, hedge_ratio_staged=0.5)


def test_oco_bracket_surface_validates_and_reaches_engine() -> None:
    params = Settings(
        entry_mode="oco_bracket",
        oco_buffer_mode="fixed_pips",
        oco_buffer_value=5,
        oco_expiry_bars=2,
        allow_reentry=True,
    ).engine_params()
    assert params.oco_buffer_mode == "fixed_pips"
    assert params.oco_buffer_value == pytest.approx(5)
    assert params.oco_expiry_bars == 2
    assert params.allow_reentry is True
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings(oco_expiry_bars=0)


def test_point_value_is_configurable_and_not_inferred_from_pip_size() -> None:
    settings = Settings(pip_size=0.01, point_value=2.5)
    params = settings.engine_params()
    assert params.pip_size == pytest.approx(0.01)
    assert params.point_value == pytest.approx(2.5)
    inferred = Settings(pip_size=0.01)
    assert inferred.point_value == pytest.approx(1.0)


def test_cost_surface_rejects_unknown_override_keys() -> None:
    with pytest.raises(ValidationError, match="unknown keys"):
        Settings(session_cost_overrides={"london": {"mystery_cost": 1.0}})
    with pytest.raises(ValidationError, match="unknown session"):
        Settings(session_cost_overrides={"sydney": {"spread_pips_per_side": 1.0}})


def test_cost_surface_rejects_invalid_rollover_timezone_and_time() -> None:
    with pytest.raises(ValidationError, match="SWAP_TIMEZONE"):
        Settings(swap_timezone="Not/A_Zone")
    with pytest.raises(ValidationError, match="SWAP_ROLLOVER_TIME"):
        Settings(swap_rollover_time="25:99")


def test_cost_surface_reaches_engine_params() -> None:
    params = Settings(
        spread_pips_per_side=2.0,
        session_cost_overrides={"london": {"spread_pips_per_side": 3.0}},
    ).engine_params()
    assert params.cost_model == "per_session"
    assert params.spread_pips_per_side == pytest.approx(2.0)
    assert params.session_cost_overrides["london"]["spread_pips_per_side"] == 3.0


def test_fixed_fractional_gets_the_default_cash_rate() -> None:
    """Sizing needs cash whatever the display unit, so the default rate applies."""
    params = Settings(risk_mode="fixed_fractional").engine_params()

    assert params.risk_mode == "fixed_fractional"
    assert params.dollars_per_pip_per_qty == pytest.approx(10.0)
    assert params.performance_unit == "pips"


def test_risk_surface_reaches_engine_params() -> None:
    params = Settings(
        risk_mode="fixed_fractional",
        risk_pct_per_r=0.1,
        max_pair_risk_pct=0.2,
        max_open_risk_pct=0.75,
        max_concurrent_structures=3,
        one_open_per_session=True,
    ).engine_params()
    assert params.risk_mode == "fixed_fractional"
    assert params.risk_pct_per_r == pytest.approx(0.1)
    assert params.max_pair_risk_pct == pytest.approx(0.2)
    assert params.max_open_risk_pct == pytest.approx(0.75)
    assert params.max_concurrent_structures == 3
    assert params.one_open_per_session is True


def test_custom_firm_profile_gets_the_default_cash_rate() -> None:
    params = Settings(firm_profile="custom").engine_params()

    assert params.firm_profile == "custom"
    assert params.dollars_per_pip_per_qty == pytest.approx(10.0)


def test_firm_profile_validates_clock_and_timezone() -> None:
    with pytest.raises(ValidationError, match="FIRM_DAILY_RESET_TIME"):
        Settings(firm_daily_reset_time="24:01")
    with pytest.raises(ValidationError, match="FIRM_TIMEZONE"):
        Settings(firm_timezone="Not/A_Zone")


def test_firm_profile_defaults_initial_balance_to_initial_capital() -> None:
    params = Settings(
        initial_capital=125_000,
        firm_profile="custom",
    ).engine_params()
    assert params.firm_initial_balance == pytest.approx(125_000)


def test_time_exit_defaults_and_overrides_reach_engine() -> None:
    default = Settings().engine_params()
    assert default.time_exit_mode == "max_age"
    assert default.max_age_hours == pytest.approx(24.0)
    disabled = Settings(time_exit_mode="none", max_age_hours=48).engine_params()
    assert disabled.time_exit_mode == "none"
    assert disabled.max_age_hours == pytest.approx(48.0)
