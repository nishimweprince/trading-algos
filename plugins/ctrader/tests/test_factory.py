from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr
from ta_plugin_api import EXECUTION_GROUP, load_providers

from ta_plugin_ctrader import FACTORY


def test_published_as_ctrader_in_the_execution_group() -> None:
    assert load_providers(EXECUTION_GROUP, ["ctrader"]) == {"ctrader": FACTORY}


def test_reports_missing_credentials_by_environment_name() -> None:
    settings = SimpleNamespace(
        client_id=SecretStr("id"),
        client_secret=None,
        access_token=None,
    )

    assert FACTORY.missing_settings(settings) == [
        "CTRADER_CLIENT_SECRET",
        "CTRADER_ACCESS_TOKEN",
    ]
