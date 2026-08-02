from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.time import to_utc

client = TestClient(app)


def test_to_utc_naive_assumes_utc():
    naive = datetime(2026, 6, 1, 0, 0, 0)
    result = to_utc(naive)
    assert result.tzinfo is not None
    assert result.hour == 0


def test_to_utc_offset_equivalent():
    aware = datetime(2026, 6, 2, 17, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    utc = datetime(2026, 6, 2, 22, 0, 0, tzinfo=timezone.utc)
    assert to_utc(aware) == to_utc(utc)


def test_trade_submit_mixed_naive_and_aware_timestamps():
    """Regression: naive date_from/date_to + offset signal_ts must not 500."""
    r = client.get("/symbols")
    symbols = r.json()
    symbol = "XAUUSD" if "XAUUSD" in symbols else "EURUSD"

    r = client.get(
        "/candles",
        params={
            "symbol": symbol,
            "timeframe": "H1",
            "date_from": "2026-06-01T00:00:00Z",
            "date_to": "2026-06-26T23:59:59Z",
        },
    )
    if r.status_code != 200 or not r.json():
        pytest.skip(f"No candle data for {symbol}")

    r = client.post(
        "/trades",
        json={
            "symbol": symbol,
            "timeframe": "H1",
            "signal_ts": "2026-06-02T17:00:00-05:00",
            "setup_id": "bear_engulfing",
            "side": -1,
            "entry": 4475.93,
            "sl": 4500,
            "tp": 4450,
            "date_from": "2026-05-31T19:00:00",
            "date_to": "2026-07-31T18:59:59",
        },
    )
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["source"] == "manual"
    else:
        assert "signal_ts" in r.json()["detail"].lower() or "candles" in r.json()["detail"].lower()


def test_candles_emit_utc_z_suffix():
    r = client.get(
        "/candles",
        params={
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 200
    candles = r.json()
    if candles:
        assert candles[0]["ts"].endswith("Z") or "+00:00" in candles[0]["ts"]


def test_session_response_utc_timestamps():
    r = client.post(
        "/sessions",
        json={
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-01-31T23:59:59Z",
            "blinded": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["date_from"].endswith("Z")
    assert body["date_to"].endswith("Z")
