"""Base rates over the bar store.

The dangerous failure here is not a wrong number, it is a confident one: bars
overlap almost completely, so a raw interval over thousands of rows looks
authoritative and means nothing. These tests pin the correction, the relaxation
order, and the guards that keep unresolved bars out of the sample.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
          ts TIMESTAMPTZ, atr_at_bar DOUBLE,
          context_reliable BOOLEAN, session_overlap BOOLEAN,
          level_touch VARCHAR, bar_tags VARCHAR, tag_setup_ids VARCHAR, {columns}
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
    tags: list[dict] | str | None = None,
    hours_step: int = 1,
) -> None:
    row = dict(CONTEXT)
    row.update(overrides or {})
    forward: list = []
    for _ in settings.feature_horizons:
        forward += [complete, 1.8, -0.6]

    bar_tags = (
        tags
        if isinstance(tags, str)
        else json.dumps({"version": settings.bar_feature_version, "tags": tags or []})
    )
    setup_ids = "" if isinstance(tags, str) else ",".join(t["setup_id"] for t in tags or [])
    placeholders = ", ".join("?" * (10 + len(DIMENSIONS) + len(forward)))
    existing = int(con.execute("SELECT count(*) FROM bar_features").fetchone()[0])
    start = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=existing)
    con.executemany(
        f"INSERT INTO bar_features VALUES ({placeholders})",
        [
            [
                "XAUUSD",
                "H1",
                version or settings.bar_feature_version,
                start + timedelta(hours=i * hours_step),
                10.0,
                reliable,
                row["session_overlap"],
                _touch(up, down),
                bar_tags,
                setup_ids,
                *[row[d] for d in DIMENSIONS],
                *forward,
            ]
            for i in range(n)
        ],
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
        "min_periods": 1,
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


def test_interval_uses_market_weeks_instead_of_raw_rows():
    """The compatibility interval may not pretend adjacent H1 rows are independent."""
    con = _con()
    _insert(con, 120, up=4, down=9)
    _insert(con, 120, up=9, down=4)

    result = _run(con)
    assert result["decided"] == 240
    assert result["effective_n"] == pytest.approx(3.0)
    assert result["independent_periods"] == 3

    from app.services.compare import wilson_interval

    naive_low, naive_high = wilson_interval(120, 240)
    assert result["wilson_high"] - result["wilson_low"] > (naive_high - naive_low) * 3


def test_timeout_is_zero_r_in_expectancy_denominator():
    con = _con()
    _insert(con, 20, up=4, down=None)
    _insert(con, 20, up=None, down=None)

    result = _run(con, apply_cost=False)

    assert result["resolved_count"] == 40
    assert result["win_rate"] == pytest.approx(0.5)
    assert result["expectancy_r"] == pytest.approx(20 * 1.5 / 40)


def test_week_gate_relaxes_until_both_requirements_are_met():
    con = _con()
    _insert(con, 220, up=4, down=9, hours_step=24)

    result = _run(con, min_samples=200, min_periods=20)

    assert result["resolved_count"] == 220
    assert result["independent_periods"] >= 20
    assert result["level_used"] != "no_signal"


def test_bootstrap_interval_is_deterministic():
    con = _con()
    _insert(con, 110, up=4, down=9, hours_step=48)
    _insert(con, 110, up=9, down=4, hours_step=48)

    first = _run(con, min_samples=200, min_periods=20, apply_cost=False)
    second = _run(con, min_samples=200, min_periods=20, apply_cost=False)

    assert first["confidence_method"] == "two_week_moving_block_bootstrap"
    assert first["net_expectancy_ci_low_r"] == second["net_expectancy_ci_low_r"]
    assert first["net_expectancy_ci_high_r"] == second["net_expectancy_ci_high_r"]


def test_relaxed_context_reports_requested_and_dropped_dimensions():
    con = _con()
    _insert(con, 5, up=4, down=9)
    _insert(con, 20, up=4, down=9, overrides={"session_overlap": True})

    result = _run(con, min_samples=20)

    assert result["fallback_used"] is True
    assert result["requested_dimensions"] == BAR_RELAX_ORDER[1:]
    assert result["dropped_dimensions"] == ["session_overlap"]


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
    assert BAR_RELAX_ORDER[:2] == ["tag_setup_id", "session_overlap"]
    assert result["dimensions_used"][0] == BAR_RELAX_ORDER[2]


def test_complete_pattern_filter_monotonically_narrows_the_population():
    con = _con()
    complete = [
        {
            "setup_id": "double_bottom",
            "state": "complete",
            "confidence": 0.83,
            "source": "algorithm",
        }
    ]
    _insert(con, 12, up=4, down=9, tags=complete)
    _insert(con, 18, up=4, down=9)

    unfiltered = _run(con, min_samples=1)
    filtered = _run(
        con,
        context={
            **CONTEXT,
            "tag_setup_id": "double_bottom",
            "tag_state": "complete",
        },
        min_samples=1,
    )

    assert filtered["matched_count"] == 12
    assert filtered["matched_count"] <= unfiltered["matched_count"]
    assert filtered["dimensions_used"][0] == "tag_setup_id"


def test_pattern_state_filter_is_exact_and_malformed_json_is_ignored():
    con = _con()
    _insert(
        con,
        7,
        up=4,
        down=9,
        tags=[{"setup_id": "double_bottom", "state": "complete"}],
    )
    _insert(
        con,
        5,
        up=4,
        down=9,
        tags=[{"setup_id": "double_bottom", "state": "forming"}],
    )
    _insert(con, 9, up=4, down=9, tags="{not-json")

    def tagged(state: str) -> dict:
        return _run(
            con,
            context={
                **CONTEXT,
                "tag_setup_id": "double_bottom",
                "tag_state": state,
            },
            min_samples=1,
        )

    assert tagged("complete")["matched_count"] == 7
    assert tagged("forming")["matched_count"] == 5
    assert tagged("any")["matched_count"] == 12


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


def test_grid_cells_match_individual_lookups():
    """A cell and a direct call must agree, or the frozen prior describes
    something other than what the panel showed."""
    from app.services.base_rate import base_rate_grid

    con = _con()
    _insert(con, 30, up=4, down=9)
    _insert(con, 20, up=9, down=4)

    grid = base_rate_grid(con, symbol="XAUUSD", timeframe="H1", context=CONTEXT, min_samples=10)
    assert len(grid["cells"]) == len(settings.touch_levels) ** 2

    for target, stop in [(1.5, 1.0), (2.0, 1.0), (0.5, 0.5)]:
        cell = next(
            c for c in grid["cells"] if c["target_atr"] == target and c["stop_atr"] == stop
        )
        direct = _run(con, target_atr=target, stop_atr=stop)
        assert (cell["wins"], cell["decided"]) == (direct["wins"], direct["decided"])


def test_the_grid_resolves_one_level_for_every_cell():
    """Every cell must describe the same population.

    Re-running the ladder per cell would silently give each its own match set,
    which reads like a grid and is twenty-five unrelated answers.
    """
    from app.services.base_rate import base_rate_grid

    con = _con()
    _insert(con, 5, up=4, down=9)
    _insert(con, 40, up=4, down=9, overrides={"session_overlap": True})

    grid = base_rate_grid(con, symbol="XAUUSD", timeframe="H1", context=CONTEXT, min_samples=20)
    assert "session_overlap" not in grid["dimensions_used"]
    assert {c["decided"] for c in grid["cells"]} == {45}


def test_target_grid_rides_along_with_a_base_rate():
    con = _con()
    _insert(con, 30, up=4, down=9)

    result = _run(con)
    assert [c["target_atr"] for c in result["target_grid"]] == list(settings.touch_levels)
    # Same stop throughout — the grid answers "was my target wrong", not "was my
    # stop wrong".
    assert {c["stop_atr"] for c in result["target_grid"]} == {1.0}


def test_unsupported_levels_are_rejected():
    con = _con()
    _insert(con, 30, up=4, down=9)

    with pytest.raises(ValueError, match="target_atr"):
        _run(con, target_atr=1.234)
    with pytest.raises(ValueError, match="horizon"):
        _run(con, horizon=7)
