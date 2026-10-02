from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from ta_core import ServiceError

from ta_plugin_binance_futures.account import AccountReader
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.testing import FakeBinanceFutures, settings


def reader(fake: FakeBinanceFutures, **overrides) -> tuple[AccountReader, httpx.AsyncClient]:
    config = settings(**overrides)
    http = httpx.AsyncClient(
        base_url=config.binance_futures_rest_url, transport=httpx.MockTransport(fake.handler)
    )
    return AccountReader(FapiRest(config, http=http, wall_clock_ms=lambda: 1)), http


async def test_signed_reads_carry_key_and_signature() -> None:
    fake = FakeBinanceFutures()
    account, http = reader(
        fake,
        binance_futures_api_key=SecretStr("k" * 64),
        binance_futures_api_secret=SecretStr("s" * 64),
    )
    rate = await account.commission_rate("BTCUSDT")
    assert rate.maker_bp == pytest.approx(2.0)
    assert rate.taker_bp == pytest.approx(5.0)
    config = await account.account_config()
    assert config["feeTier"] == 0 and config["canWithdraw"] is False
    request = fake.requests[-1]
    assert request.headers["X-MBX-APIKEY"] == "k" * 64
    assert request.url.params["timestamp"] == "1"
    assert len(request.url.params["signature"]) == 64
    # The secret itself never goes over the wire.
    assert "s" * 64 not in str(request.url)
    await http.aclose()


async def test_without_key_signed_reads_refuse() -> None:
    account, http = reader(FakeBinanceFutures())
    assert not account.available
    with pytest.raises(ServiceError) as caught:
        await account.commission_rate("BTCUSDT")
    assert caught.value.status_code == 503
    assert await account.timed(signed=False) >= 0
    await http.aclose()


async def test_key_restrictions_flag_dangerous_permissions() -> None:
    from ta_plugin_binance_futures.account import AccountReader

    fake = FakeBinanceFutures()
    config = settings(
        binance_futures_api_key=SecretStr("k" * 64),
        binance_futures_api_secret=SecretStr("s" * 64),
    )
    http = httpx.AsyncClient(
        base_url="https://api.binance.test", transport=httpx.MockTransport(fake.handler)
    )
    sapi = FapiRest(config, http=http)
    reader = AccountReader(FapiRest(config, http=http), sapi=sapi)
    clean = await reader.api_restrictions()
    assert clean is not None and clean.read_only and clean.ip_restricted and clean.dangerous == ()
    fake.key_restrictions["enableFutures"] = True
    fake.key_restrictions["enableWithdrawals"] = True
    fake.key_restrictions["ipRestrict"] = False
    risky = await reader.api_restrictions()
    assert risky is not None and not risky.read_only and not risky.ip_restricted
    assert set(risky.dangerous) == {"enableFutures", "enableWithdrawals"}
    assert fake.requests[-1].url.path == "/sapi/v1/account/apiRestrictions"
    # No spot client (or no key): the check is skipped, not failed.
    assert await AccountReader(FapiRest(config, http=http)).api_restrictions() is None
    await http.aclose()
