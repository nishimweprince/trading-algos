"""Capturing the prior alongside the annotation.

Two things have to hold. The prior must be frozen at annotation time, because a
rethreshold or a rebuilt store later would otherwise silently change what the
operator is recorded as having decided against. And it must never be able to fail
the write: the feature store is a build artifact that may not exist yet, and
losing a signal mid-session because nobody ran the builder would be a far worse
outcome than a missing number.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.duck import get_connection, register_candles_view, register_features_view
from app.main import app
from app.services.base_rate import store_is_built
from app.services.signals import freeze_base_rate

SIGNAL_TS = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def con():
    connection = get_connection()
    register_candles_view(connection)
    register_features_view(connection)
    yield connection
    connection.close()


def _has_data(con) -> bool:
    row = con.execute(
        "SELECT count(*) FROM candles WHERE symbol = 'XAUUSD' AND timeframe = 'H1'"
    ).fetchone()
    return bool(row and row[0]) and store_is_built(con)


def test_frozen_prior_matches_a_live_lookup(con):
    """The grid cell and /base-rate must agree at the same bar and geometry."""
    if not _has_data(con):
        pytest.skip("no XAUUSD H1 feature store built")

    client = TestClient(app)
    body = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "signal_ts": SIGNAL_TS.isoformat(),
        "setup_id": "double_bottom",
        "side": 1,
        "compare_min_samples": 20,
    }
    signal = client.post("/signals", json=body).json()
    grid = signal["base_rate_at_signal"]
    assert grid is not None

    cell = next(c for c in grid["cells"] if c["target_atr"] == 1.5 and c["stop_atr"] == 1.0)
    live = client.get(
        "/base-rate",
        params={
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "signal_ts": SIGNAL_TS.isoformat(),
            "target_atr": 1.5,
            "stop_atr": 1.0,
            "side": 1,
            "min_samples": 20,
        },
    ).json()

    assert (cell["wins"], cell["decided"]) == (live["wins"], live["decided"])
    assert grid["level_used"] == live["level_used"]


def test_a_missing_store_leaves_the_prior_null_without_raising(con, monkeypatch):
    """The signal is the record; the prior is a nice-to-have attached to it."""
    monkeypatch.setattr("app.services.base_rate.store_is_built", lambda _con: False)
    assert (
        freeze_base_rate(con, "XAUUSD", "H1", SIGNAL_TS, {"trend_state": "up"}, side=1) is None
    )


def test_a_failing_lookup_does_not_take_the_signal_with_it(con, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("feature store is corrupt")

    monkeypatch.setattr("app.services.base_rate.base_rate_grid", boom)
    assert (
        freeze_base_rate(con, "XAUUSD", "H1", SIGNAL_TS, {"trend_state": "up"}, side=1) is None
    )
