"""Base rates over the bar store.

The dangerous failure here is not a wrong number, it is a confident one: bars
overlap almost completely, so a raw interval over thousands of rows looks
authoritative and means nothing. These tests pin the correction, the relaxation
order, and the guards that keep unresolved bars out of the sample.
"""

from __future__ import annotations

import duckdb
import pytest

from app.config import settings
from app.services.base_rate import BAR_RELAX_ORDER, base_rate

DIMENSIONS = [
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "htf_atr_bucket",
]

CONTEXT = {
    "trend_state": "up",
    "atr_bucket": "mid",
    "session": "london",
    "rsi_band": "neutral",
    "day_of_week": "tue",
    "ema_slope_bucket": "up",
    "atr_change_bucket": "stable",
    "htf_trend_state": "up",
    "htf_atr_bucket": "mid",
    "session_overlap": False,
}


def _forward_columns() -> list[str]:
    """One set of forward columns per configured horizon, as the builder writes."""
    cols = []
    for h in settings.feature_horizons:
        cols += [f"fwd{h}_complete BOOLEAN", f"fwd{h}_max_atr DOUBLE", f"fwd{h}_min_atr DOUBLE"]
    return cols


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    columns = ", ".join([f"{d} VARCHAR" for d in DIMENSIONS] + _forward_columns())
    con.execute(
        f"""
        CREATE TABLE bar_features (
          symbol VARCHAR, timeframe VARCHAR, bar_feature_version VARCHAR,
          context_reliable BOOLEAN, session_overlap BOOLEAN,
          level_touch VARCHAR, {columns}
        )
        """
    )
    return con


def _touch(up: int | None, down: int | None) -> str:
    def j(v):
        return "null" if v is None else str(v)

    levels = ",".join(f'"{m:.1f}":{{"up":{j(up)},"down":{j(down)}}}' for m in settings.touch_levels)
    return "{" + levels + "}"


def _insert(
    con,
    n: int,
    up: int | None,
    down: int | None,
    *,
    complete: bool = True,
    reliable: bool = True,
    version: str | None = None,
    overrides: dict | None = None,
) -> None:
    row = dict(CONTEXT)
    row.update(overrides or {})
    forward: list = []
    for _ in settings.feature_horizons:
        forward += [complete, 1.8, -0.6]

    placeholders = ", ".join("?" * (6 + len(DIMENSIONS) + len(forward)))
    con.executemany(
        f"INSERT INTO bar_features VALUES ({placeholders})",
        [
            [
                "XAUUSD",
                "H1",
                version or settings.bar_feature_version,
                reliable,
                row["session_overlap"],
                _touch(up, down),
                *[row[d] for d in DIMENSIONS],
                *forward,
            ]
        ]
        * n,
    )


def _run(con, **kwargs) -> dict:
    params = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "context": CONTEXT,
        "horizon": 24,
        "target_atr": 1.5,
        "stop_atr": 1.0,
        "side": 1,
        "min_samples": 10,
    }
    params.update(kwargs)
    return base_rate(con, **params)


def test_wins_and_losses_come_from_first_touch_order():
    con = _con()
    _insert(con, 30, up=4, down=9)  # target first -> win
    _insert(con, 20, up=9, down=4)  # stop first -> loss

    result = _run(con)
    assert result["wins"] == 30
    assert result["losses"] == 20
    assert result["decided"] == 50
    assert result["win_rate"] == pytest.approx(0.6)
    # A win pays target/stop against a stop costing 1R.
    assert result["expectancy_r"] == pytest.approx((30 * 1.5 - 20) / 50)


def test_timeouts_are_counted_but_not_decided():
    con = _con()
    _insert(con, 20, up=4, down=None)
    _insert(con, 15, up=None, down=None)  # neither barrier reached in 48 bars

    result = _run(con)
    assert result["decided"] == 20
    assert result["timeouts"] == 15
    assert result["matched_count"] == 35


def test_a_barrier_reached_after_the_horizon_does_not_count():
    """The store scans 48 bars; a 24-bar query must ignore bars 25-48.

    Without the horizon guard these 20 rows would read as wins, and every shorter
    horizon would inherit the longest one's outcomes.
    """
    con = _con()
    _insert(con, 20, up=40, down=None)  # target reached, but at bar 40
    _insert(con, 15, up=4, down=9)  # genuinely decided inside the horizon

    result = _run(con)
    assert result["decided"] == 15
    assert result["wins"] == 15
    assert result["timeouts"] == 20

    # The same rows at the 48-bar horizon do resolve.
    longer = _run(con, horizon=48)
    assert longer["wins"] == 35


def test_interval_widens_to_the_effective_sample_size():
    """Adjacent bars share 23 of 24 forward bars, so 240 rows are ~10 draws.

    Without this the interval would be reported as if every bar were an
    independent observation, which is the single easiest way for this store to
    produce a confident lie.
    """
    con = _con()
    _insert(con, 120, up=4, down=9)
    _insert(con, 120, up=9, down=4)

    result = _run(con)
    assert result["decided"] == 240
    assert result["effective_n"] == pytest.approx(10.0)

    from app.services.compare import wilson_interval

    naive_low, naive_high = wilson_interval(120, 240)
    assert result["wilson_high"] - result["wilson_low"] > (naive_high - naive_low) * 3


def test_unresolved_and_unreliable_bars_are_excluded():
    con = _con()
    _insert(con, 30, up=4, down=9)
    _insert(con, 50, up=9, down=4, complete=False)
    _insert(con, 50, up=9, down=4, reliable=False)
    _insert(con, 50, up=9, down=4, version="0.0.1-old")

    result = _run(con)
    assert result["decided"] == 30
    assert result["losses"] == 0


def test_dimensions_are_dropped_in_relax_order():
    """Only bars differing on the first-dropped dimension are added, so the
    sample can only reach the threshold if that dimension went first."""
    con = _con()
    _insert(con, 5, up=4, down=9)
    _insert(con, 20, up=4, down=9, overrides={"session_overlap": True})

    result = _run(con, min_samples=20)
    assert result["decided"] == 25
    assert "session_overlap" not in result["dimensions_used"]
    assert result["dimensions_used"][0] == BAR_RELAX_ORDER[1]


def test_a_pinned_dimension_is_never_dropped():
    con = _con()
    _insert(con, 5, up=4, down=9)
    _insert(con, 40, up=4, down=9, overrides={"session_overlap": True})

    result = _run(con, min_samples=20, pinned=["session_overlap"])
    assert result["level_used"] == "no_signal"
    assert result["decided_available"] == 5


def test_short_side_reads_the_mirrored_barriers():
    con = _con()
    # Price fell: the down level came first, which is a win for a short.
    _insert(con, 30, up=9, down=4)

    long_result = _run(con, side=1)
    short_result = _run(con, side=-1)
    assert long_result["wins"] == 0
    assert short_result["wins"] == 30
    # Excursions flip with the side too.
    assert short_result["median_mfe_atr"] == pytest.approx(0.6)
    assert short_result["median_mae_atr"] == pytest.approx(-1.8)


def test_unsupported_levels_are_rejected():
    con = _con()
    _insert(con, 30, up=4, down=9)

    with pytest.raises(ValueError, match="target_atr"):
        _run(con, target_atr=1.234)
    with pytest.raises(ValueError, match="horizon"):
        _run(con, horizon=7)
