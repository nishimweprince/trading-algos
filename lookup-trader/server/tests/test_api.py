from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_symbols_and_setups():
    r = client.get("/symbols")
    assert r.status_code == 200
    assert "EURUSD" in r.json()

    r = client.get("/setups")
    assert r.status_code == 200
    setups = r.json()
    assert len(setups) >= 4


def test_candles_and_session_flow():
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
    assert len(candles) >= 1

    r = client.post(
        "/sessions",
        json={
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
            "blinded": False,
        },
    )
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    signal_ts = candles[0]["ts"]
    r = client.post(
        "/trades",
        json={
            "session_id": session_id,
            "symbol": "EURUSD",
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "setup_id": "bull_engulfing",
            "side": 1,
            "entry": 1.1002,
            "sl": 1.0990,
            "tp": 1.1020,
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 200
    trade = r.json()
    assert trade["source"] == "manual"
    assert trade["result"] in ("win", "loss", "timeout", "ambiguous")

    r = client.post(
        "/compare",
        json={
            "setup_id": "bull_engulfing",
            "symbol": "EURUSD",
            "timeframe": "H1",
            "context": {"trend_state": "up", "session": "asian"},
            "min_samples": 1,
        },
    )
    assert r.status_code == 200
    assert "level_used" in r.json()
