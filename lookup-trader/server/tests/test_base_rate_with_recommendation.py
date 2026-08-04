"""base_rate_with_recommendation — dual-side pick and response shape."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.duck import get_connection, register_candles_view, register_features_view
from app.main import app
from app.services.base_rate import base_rate_with_recommendation, store_is_built

from tests.test_base_rate import CONTEXT, _con, _insert

client = TestClient(app)


def _run_rec(con, **kwargs) -> dict:
    params = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "context": CONTEXT,
        "horizon": 24,
        "target_atr": 1.5,
        "stop_atr": 1.0,
        "min_samples": 10,
    }
    params.update(kwargs)
    return base_rate_with_recommendation(con, **params)


def test_explicit_side_attaches_recommendation_fields():
    con = _con()
    _insert(con, 30, up=4, down=9)

    result = _run_rec(con, side=1)

    assert result["scored_side"] == 1
    assert result["scored_direction"] == "long"
    assert result["recommendation"] is not None
    assert result["recommendation"]["verdict"] in ("buy", "sell", "wait", "insufficient_data")
    assert "headline" in result["recommendation"]
    assert "rationale" in result["recommendation"]


def test_dual_side_picks_long_when_only_long_is_favorable():
    """Long wins on bars where target is touched first; short loses on the same rows."""
    con = _con()
    _insert(con, 120, up=4, down=9)

    result = _run_rec(con, side=None)

    assert result["scored_side"] == 1
    assert result["scored_direction"] == "long"
    assert result["recommendation"]["verdict"] == "buy"


def test_dual_side_returns_winner_when_both_sides_scored():
    con = _con()
    _insert(con, 30, up=4, down=9)  # long wins
    _insert(con, 30, up=9, down=4)  # short wins

    long_only = _run_rec(con, side=1)
    short_only = _run_rec(con, side=-1)

    dual = _run_rec(con, side=None)

    assert dual["scored_side"] in (1, -1)
    assert dual["recommendation"] is not None
    # Winner should match the side with the stronger verdict rank.
    candidates = {1: long_only, -1: short_only}
    from app.services.recommendation import verdict_rank

    def key(item: dict) -> tuple:
        rec = item["recommendation"]
        exp = item.get("expectancy_r_net") or item.get("expectancy_r") or -99.0
        return (
            verdict_rank(rec["verdict"]),
            exp,
            item.get("wilson_low") or -99.0,
        )

    expected_side = max(candidates, key=lambda s: key(candidates[s]))
    assert dual["scored_side"] == expected_side


def test_no_signal_still_returns_recommendation():
    con = _con()
    _insert(con, 3, up=4, down=9)

    result = _run_rec(con, side=1, min_samples=10)

    assert result["level_used"] == "no_signal"
    assert result["recommendation"]["verdict"] == "insufficient_data"


@pytest.fixture
def live_con():
    connection = get_connection()
    register_candles_view(connection)
    register_features_view(connection)
    yield connection
    connection.close()


def test_api_base_rate_without_side_returns_recommendation(live_con):
    if not store_is_built(live_con):
        pytest.skip("no feature store built")

    row = live_con.execute(
        """
        SELECT ts FROM bar_features
        WHERE symbol = 'XAUUSD' AND timeframe = 'H1'
          AND bar_feature_version = ?
          AND fwd24_complete IS TRUE
        LIMIT 1
        """,
        [settings.bar_feature_version],
    ).fetchone()
    if not row:
        pytest.skip("no complete XAUUSD H1 rows in feature store")

    signal_ts = row[0]
    if hasattr(signal_ts, "isoformat"):
        signal_ts = signal_ts.isoformat()

    response = client.get(
        "/base-rate",
        params={
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "target_atr": 1.5,
            "stop_atr": 1.0,
            "min_samples": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scored_side"] in (1, -1)
    assert body["scored_direction"] in ("long", "short")
    assert body["recommendation"] is not None
    assert body["recommendation"]["verdict"] in ("buy", "sell", "wait", "insufficient_data")
