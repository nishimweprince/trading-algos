"""The reveal gate on the overlay endpoint.

The context half of a feature row is safe to draw at any time. The forward half
summarises bars the replay may still be hiding, so it is withheld server-side —
a response carrying values the UI merely declines to render would be one network
tab away from defeating the whole exercise.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from app.config import settings
from app.services.bar_series import CONTEXT_FIELDS, fetch_bar_series, horizon_cutoff

HORIZON = 24
START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _con(bars: int = 120) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    forward = ", ".join(
        f"fwd{h}_{name} {'BOOLEAN' if name in ('complete', 'max_first') else 'DOUBLE'}"
        for h in settings.feature_horizons
        for name in ("max_atr", "min_atr", "bars_to_max", "bars_to_min", "max_first", "complete")
    )
    con.execute(
        f"""
        CREATE TABLE bar_features (
          symbol VARCHAR, timeframe VARCHAR, ts TIMESTAMPTZ,
          bar_feature_version VARCHAR, level_touch VARCHAR,
          trend_state VARCHAR, atr_bucket VARCHAR, session VARCHAR, rsi_band VARCHAR,
          atr_at_bar DOUBLE, atr_pct DOUBLE, efficiency_ratio DOUBLE,
          close_range_pct DOUBLE, realized_vol_atr DOUBLE, context_reliable BOOLEAN,
          {forward}
        )
        """
    )
    values = []
    for i in range(bars):
        ts = START + timedelta(hours=i)
        fwd: list = []
        for _ in settings.feature_horizons:
            fwd += [1.8, -0.6, 3, 7, True, True]
        values.append(
            [
                "XAUUSD",
                "H1",
                ts,
                settings.bar_feature_version,
                json.dumps({"1.0": {"up": 3, "down": 7}}),
                "up",
                "mid",
                "london",
                "neutral",
                2.5,
                0.001,
                0.42,
                0.6,
                1.1,
                True,
                *fwd,
            ]
        )
    placeholders = ", ".join("?" * len(values[0]))
    con.executemany(f"INSERT INTO bar_features VALUES ({placeholders})", values)
    return con


def _fetch(con, revealed_through=None):
    return fetch_bar_series(
        con,
        symbol="XAUUSD",
        timeframe="H1",
        date_from=START,
        date_to=START + timedelta(hours=200),
        revealed_through=revealed_through,
        horizon=HORIZON,
    )


def test_forward_half_is_withheld_up_to_the_cutoff():
    """Bar i becomes readable only once the cursor has reached i + horizon."""
    con = _con()
    reveal = START + timedelta(hours=60)
    rows = _fetch(con, reveal)

    cutoff = horizon_cutoff("H1", reveal, HORIZON)
    assert cutoff == reveal - timedelta(hours=HORIZON)

    for row in rows:
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        assert row["forward_visible"] == (ts <= cutoff), row["ts"]
        if row["forward_visible"]:
            assert row["max_atr"] is not None
        else:
            assert all(row[f] is None for f in ("max_atr", "min_atr", "bars_to_max", "max_first"))


def test_context_is_returned_for_every_bar_including_hidden_ones():
    """The gate covers the forward half only — withholding context would make the
    strip blank exactly where the operator is looking."""
    con = _con()
    rows = _fetch(con, START + timedelta(hours=30))
    assert rows
    for row in rows:
        for field in CONTEXT_FIELDS:
            assert row[field] is not None, field


def test_omitting_the_reveal_boundary_hides_everything_forward():
    """The safe failure: a caller that forgets the parameter gets less, not more."""
    con = _con()
    rows = _fetch(con, None)
    assert rows
    assert not any(row["forward_visible"] for row in rows)
    assert all(row["max_atr"] is None for row in rows)


def test_an_unknown_timeframe_suppresses_forward_data():
    """No known bar length means no defensible cutoff, so nothing is released."""
    assert horizon_cutoff("W1", START, HORIZON) is None


def test_unsupported_horizon_is_rejected():
    con = _con()
    with pytest.raises(ValueError, match="horizon"):
        fetch_bar_series(
            con,
            symbol="XAUUSD",
            timeframe="H1",
            date_from=START,
            date_to=START + timedelta(hours=200),
            horizon=7,
        )
