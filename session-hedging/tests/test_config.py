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
