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


def test_dollar_default_requires_conversion_rate() -> None:
    with pytest.raises(ValidationError, match="DOLLARS_PER_PIP_PER_QTY"):
        Settings(performance_unit="dollars")


def test_fixed_stop_mode_requires_a_distance() -> None:
    with pytest.raises(ValidationError, match="FIXED_STOP_PIPS"):
        Settings(stop_mode="fixed_pips")


def test_fixed_stop_pips_reaches_engine_params() -> None:
    params = Settings(stop_mode="fixed_pips", fixed_stop_pips=150).engine_params()
    assert params.stop_mode == "fixed_pips"
    assert params.fixed_stop_pips == pytest.approx(150.0)


def test_stop_mode_defaults_to_bar_range() -> None:
    assert Settings().engine_params().stop_mode == "bar_range"


def test_point_value_is_configurable_and_not_inferred_from_pip_size() -> None:
    settings = Settings(pip_size=0.01, point_value=2.5)
    params = settings.engine_params()
    assert params.pip_size == pytest.approx(0.01)
    assert params.point_value == pytest.approx(2.5)
    inferred = Settings(pip_size=0.01)
    assert inferred.point_value == pytest.approx(1.0)
