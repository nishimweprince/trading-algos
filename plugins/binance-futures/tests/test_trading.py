from __future__ import annotations

import httpx
import pytest

from ta_plugin_binance_futures.factory import EXECUTION_FACTORY
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.testing import trading_settings
from ta_plugin_binance_futures.trading import ALGO_EVENTS, FuturesTrading


def trading(handler) -> tuple[FuturesTrading, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    config = trading_settings()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url=config.binance_futures_order_rest_url
    )
    rest = FapiRest(
        config,
        http=http,
        api_key=config.binance_futures_trading_api_key,
        api_secret=config.binance_futures_trading_api_secret,
        wall_clock_ms=lambda: 1_700_000_000_000,
    )
    return FuturesTrading(rest), seen


def ok(body=None):
    return lambda request: httpx.Response(200, json=body if body is not None else {})


async def test_stop_market_close_position_goes_to_the_algo_service():
    t, seen = trading(ok({"algoId": 7, "clientAlgoId": "zt-abc-sl1"}))
    reply = await t.new_algo(
        "BTCUSDT",
        "SELL",
        "STOP_MARKET",
        trigger_price=59000.5,
        client_id="zt-abc-sl1",
        close_position=True,
    )
    assert reply.ok and reply.body["algoId"] == 7
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/fapi/v1/algoOrder"
    p = req.url.params
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET"
    assert p["triggerPrice"] == "59000.5" and p["closePosition"] == "true"
    assert p["workingType"] == "MARK_PRICE" and p["priceProtect"] == "true"
    assert "quantity" not in p and "reduceOnly" not in p
    assert "signature" in p and req.headers["X-MBX-APIKEY"]


async def test_take_profit_with_quantity_is_reduce_only():
    t, seen = trading(ok())
    await t.new_algo(
        "ETHUSDT",
        "SELL",
        "TAKE_PROFIT_MARKET",
        trigger_price=2600,
        client_id="zt-abc-tp1",
        quantity=0.012,
        reduce_only=True,
    )
    p = seen[0].url.params
    assert p["quantity"] == "0.012" and p["reduceOnly"] == "true" and "closePosition" not in p


async def test_close_position_cannot_carry_a_quantity():
    t, _ = trading(ok())
    with pytest.raises(ValueError):
        await t.new_algo(
            "BTCUSDT",
            "SELL",
            "STOP_MARKET",
            trigger_price=1,
            client_id="x",
            close_position=True,
            quantity=1,
        )


async def test_market_order_asks_for_the_full_result():
    t, seen = trading(ok({"status": "FILLED"}))
    await t.new_order("BTCUSDT", "BUY", "MARKET", quantity=0.001, client_id="zt-abc-entry")
    p = seen[0].url.params
    assert p["newOrderRespType"] == "RESULT" and p["newClientOrderId"] == "zt-abc-entry"
    assert p["quantity"] == "0.001" and "reduceOnly" not in p


@pytest.mark.parametrize(
    "response, outcome, code",
    [
        (
            httpx.Response(400, json={"code": -2021, "msg": "would immediately trigger"}),
            "rejected",
            -2021,
        ),
        (httpx.Response(503, json={"code": -1001}), "unknown", None),
        (httpx.Response(200, json={}), "ok", None),
    ],
)
async def test_outcomes(response, outcome, code):
    t, _ = trading(lambda request: response)
    reply = await t.cancel_algo("zt-abc-sl1")
    assert reply.outcome == outcome and reply.code == code


async def test_transport_failure_is_unknown_not_rejected():
    def boom(request):
        raise httpx.ConnectError("down")

    t, _ = trading(boom)
    reply = await t.new_order("BTCUSDT", "BUY", "MARKET", quantity=1, client_id="c")
    assert reply.outcome == "unknown"


async def test_rate_limit_backoff_is_not_sent():
    t, seen = trading(lambda r: httpx.Response(429, headers={"retry-after": "30"}, json={}))
    first = await t.positions()
    second = await t.positions()
    assert first.outcome == "rejected" and second.outcome == "not_sent" and len(seen) == 1


async def test_margin_type_already_isolated_is_ok():
    t, _ = trading(lambda r: httpx.Response(400, json={"code": -4046, "msg": "No need"}))
    assert (await t.set_margin_type("BTCUSDT")).ok


async def test_public_calls_are_unsigned():
    t, seen = trading(ok([]))
    await t.klines("BTCUSDT", "1h", 3)
    assert "signature" not in seen[0].url.params and "X-MBX-APIKEY" not in seen[0].headers


def test_factory_builds_trading_and_algo_user_stream():
    config = trading_settings()
    t = EXECUTION_FACTORY.trading(config)
    stream = EXECUTION_FACTORY.user_stream(config, t.rest, lambda e: None, on_connected=None)
    assert stream.url("k").endswith(f"events={ALGO_EVENTS}")
