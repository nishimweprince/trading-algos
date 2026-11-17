"""Read-only broker history on the execution connection: candles, deals, position labels."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from ta_plugin_ctrader._generated.OpenApiModelMessages_pb2 import (
    ProtoOAClosePositionDetail,
    ProtoOADeal,
    ProtoOAPosition,
    ProtoOATradeData,
    ProtoOATrendbar,
)
from ta_plugin_ctrader.errors import CTraderError
from ta_plugin_ctrader.proto import ProtoOADealListRes, ProtoOAGetTrendbarsRes
from ta_store import ExecutionRepository

from execution_service.api import create_app
from tests.test_execution_service import _ready_gateway

HEADERS = {"X-API-Key": "test-api-key-at-least-16"}
T0 = int(datetime(2026, 11, 16, 10, 0, tzinfo=UTC).timestamp() * 1000)


def _client(tmp_path: Path, gateway) -> TestClient:
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    return TestClient(create_app(gateway.settings, gateway=gateway, repository=repository))


def test_positions_carry_label_comment_and_open_time(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)
    account = gateway.account("forex_demo")
    account.positions[77] = ProtoOAPosition(
        positionId=77,
        tradeData=ProtoOATradeData(
            symbolId=1,
            volume=1_000_000,
            tradeSide=1,
            label="wf_s06",
            comment="wf_s06",
            openTimestamp=T0,
        ),
        positionStatus=1,
        swap=0,
        price=1.08,
    )
    body = (
        _client(tmp_path, gateway).get("/v1/accounts/forex_demo/positions", headers=HEADERS).json()
    )
    assert body[0]["label"] == "wf_s06" and body[0]["comment"] == "wf_s06"
    assert body[0]["opened_at"].startswith("2026-11-16T10:00:00")


def test_candles_route_pages_through_the_execution_connection(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)
    sent = []

    async def fake_enqueue(environment, message, *, priority, historical, correlation_id=None):
        sent.append((message, priority, historical))
        minute0 = T0 // 60_000
        bars = [
            ProtoOATrendbar(
                low=108_000 + i,
                deltaOpen=5,
                deltaClose=7,
                deltaHigh=10,
                utcTimestampInMinutes=minute0 + i,
                volume=3,
            )
            for i in range(3)
        ]
        return ProtoOAGetTrendbarsRes(
            ctidTraderAccountId=12345678, period=1, trendbar=bars, hasMore=False
        )

    gateway._enqueue_request = fake_enqueue
    client = _client(tmp_path, gateway)
    r = client.get(
        "/v1/accounts/forex_demo/candles?instrument=eurusd&timeframe=M1&count=3", headers=HEADERS
    )
    assert r.status_code == 200, r.text
    candles = r.json()
    assert (
        len(candles) == 3
        and candles[0]["source_instrument"] == "EURUSD"
        and candles[0]["low"] == 1.08
    )
    assert sent[0][1:] == (10, True)  # low priority, historical throttle
    assert (
        client.get(
            "/v1/accounts/forex_demo/candles?instrument=EURUSD&count=9999", headers=HEADERS
        ).status_code
        == 422
    )
    assert (
        client.get("/v1/accounts/nope/candles?instrument=EURUSD", headers=HEADERS).status_code
        == 404
    )
    assert client.get("/v1/accounts/forex_demo/candles?instrument=EURUSD").status_code == 401

    async def broken(*args, **kwargs):
        raise CTraderError("NOT_CONNECTED", "down")

    gateway._enqueue_request = broken
    r = client.get("/v1/accounts/forex_demo/candles?instrument=EURUSD", headers=HEADERS)
    assert r.status_code == 503 and r.json()["error"]["code"] == "broker_unavailable"


def test_deals_route_reports_closing_deals(tmp_path: Path) -> None:
    gateway = _ready_gateway(tmp_path)
    deals = [
        ProtoOADeal(
            dealId=1,
            orderId=10,
            positionId=77,
            volume=1_000_000,
            filledVolume=1_000_000,
            symbolId=1,
            createTimestamp=T0,
            executionTimestamp=T0,
            executionPrice=1.08,
            tradeSide=1,
            dealStatus=2,
            commission=-35,
            moneyDigits=2,
        ),
        ProtoOADeal(
            dealId=2,
            orderId=11,
            positionId=77,
            volume=1_000_000,
            filledVolume=1_000_000,
            symbolId=1,
            createTimestamp=T0 + 60_000,
            executionTimestamp=T0 + 60_000,
            executionPrice=1.0790,
            tradeSide=2,
            dealStatus=2,
            commission=-35,
            moneyDigits=2,
            closePositionDetail=ProtoOAClosePositionDetail(
                entryPrice=1.08,
                grossProfit=-1000,
                swap=0,
                commission=-35,
                balance=99_000_00,
                moneyDigits=2,
            ),
        ),
    ]

    async def fake_enqueue(environment, message, *, priority, historical, correlation_id=None):
        assert message.fromTimestamp == T0 and historical
        return ProtoOADealListRes(
            ctidTraderAccountId=12345678, deal=list(reversed(deals)), hasMore=False
        )

    gateway._enqueue_request = fake_enqueue
    client = _client(tmp_path, gateway)
    r = client.get("/v1/accounts/forex_demo/deals?since=2026-11-16T10:00:00Z", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["deal_id"] for d in body] == [1, 2]
    close = body[1]
    assert close["closing"] and close["position_id"] == 77 and close["direction"] == "sell"
    assert float(close["price"]) == 1.079 and float(close["entry_price"]) == 1.08
    assert float(close["gross_profit"]) == -10.0 and float(close["commission"]) == -0.35
    assert float(close["volume_lots"]) == 0.1 and close["instrument"] == "EURUSD"
    assert not body[0]["closing"] and body[0]["entry_price"] is None
    naive = client.get("/v1/accounts/forex_demo/deals?since=2026-11-16T10:00:00", headers=HEADERS)
    assert naive.status_code == 422
