"""Comparison filtering, relaxation and the R-grid rollup.

Rows are inserted directly rather than through /trades: these tests are about
what the query does with a known population, and going through the labeler would
make the population depend on the candle fixtures.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.duck import get_connection, register_candles_view
from app.services.compare import compare_occurrences

SETUP = "bull_engulfing"
SYMBOL = "TESTFX"
TIMEFRAME = "H1"
BASE_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def con():
    connection = get_connection()
    register_candles_view(connection)
    connection.execute("DELETE FROM occurrences WHERE symbol = ?", [SYMBOL])
    try:
        yield connection
    finally:
        connection.execute("DELETE FROM occurrences WHERE symbol = ?", [SYMBOL])
        connection.close()


def add(con, index: int = 0, **overrides) -> str:
    """Insert one occurrence, defaulting to a plain winning long."""
    row = {
        "id": str(uuid.uuid4()),
        "source": "manual",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        # Spaced far enough apart that nothing overlaps unless a test wants it to.
        "ts": BASE_TS + timedelta(days=index),
        "exit_ts": BASE_TS + timedelta(days=index, hours=1),
        "setup_id": SETUP,
        "side": 1,
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "result": "win",
        "realized_r": 2.0,
        "outcome_kind": "traded",
        "context_reliable": True,
        "excluded": False,
        "peeked": False,
        "trend_state": "up",
        "session": "london",
        "atr_bucket": "mid",
        "rsi_band": "neutral",
    }
    row.update(overrides)
    if isinstance(row.get("r_grid"), dict):
        row["r_grid"] = json.dumps(row["r_grid"])

    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    con.execute(
        f"INSERT INTO occurrences ({columns}) VALUES ({placeholders})", list(row.values())
    )
    return row["id"]


def run(con, context=None, **kwargs):
    return compare_occurrences(
        con,
        setup_id=SETUP,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        context=context or {},
        min_samples=kwargs.pop("min_samples", 2),
        **kwargs,
    )


def test_longs_and_shorts_do_not_pool_when_side_is_given(con):
    """The regression this change exists for: nine seeded setups have no default
    side, so without a side filter a long and a short share one win rate."""
    for i in range(3):
        add(con, i, side=1, result="win")
    for i in range(3, 6):
        add(con, i, side=-1, result="loss", realized_r=-1.0)

    pooled = run(con)
    assert pooled["decided"] == 6
    assert pooled["win_rate"] == pytest.approx(0.5)

    longs = run(con, {"side": 1})
    assert longs["decided"] == 3
    assert longs["win_rate"] == pytest.approx(1.0)
    assert "side" in longs["level_used"]


def test_pinned_dimension_is_never_relaxed(con):
    """Pins are the contract that makes the answer defensible — if they cannot be
    met, the honest response is no_signal rather than a widened sample."""
    for i in range(5):
        add(con, i, side=1)

    # No shorts exist, so pinning side=-1 can never reach min_samples.
    pinned = run(con, {"side": -1, "session": "london"}, pinned=["side"])
    assert pinned["level_used"] == "no_signal"
    assert pinned["decided"] == 0

    # Unpinned, the same query relaxes side away and answers on the longs.
    relaxed = run(con, {"side": -1, "session": "london"})
    assert relaxed["decided"] == 5


def test_relaxation_drops_in_order_and_level_used_names_survivors(con):
    for i in range(4):
        add(con, i, session="london", rsi_band="neutral", entry_quality="clean")

    # entry_quality is dropped before rsi_band, which is dropped before session.
    result = run(
        con,
        {"session": "london", "rsi_band": "neutral", "entry_quality": "messy"},
    )
    assert result["decided"] == 4
    assert result["level_used"] == "rsi_band+session"


def test_confluence_requires_every_selected_tag(con):
    for i in range(3):
        add(con, i, confluence_tags="key_level")
    for i in range(3, 6):
        add(con, i, confluence_tags="key_level,fibonacci")

    both = run(con, {"confluence_tags": ["key_level", "fibonacci"]}, pinned=["confluence_tags"])
    assert both["decided"] == 3

    one = run(con, {"confluence_tags": ["key_level"]}, pinned=["confluence_tags"])
    assert one["decided"] == 6


def test_peeked_rows_are_excluded_by_default_and_counted(con):
    for i in range(3):
        add(con, i, peeked=False)
    for i in range(3, 6):
        add(con, i, peeked=True, result="loss", realized_r=-1.0)

    clean = run(con)
    assert clean["decided"] == 3
    assert clean["win_rate"] == pytest.approx(1.0)
    assert clean["excluded_peeked"] == 3

    everything = run(con, exclude_peeked=False)
    assert everything["decided"] == 6
    assert everything["excluded_peeked"] == 0


def test_skips_are_reported_but_never_counted_as_trades(con):
    for i in range(3):
        add(con, i)
    for i in range(3, 6):
        add(
            con,
            i,
            outcome_kind="skipped",
            result=None,
            realized_r=None,
            skip_reason="low_conviction" if i < 5 else "news_risk",
        )

    result = run(con)
    assert result["decided"] == 3
    assert result["skipped_count"] == 3
    assert result["skip_reasons"] == {"low_conviction": 2, "news_risk": 1}


def test_target_grid_scores_alternative_targets(con):
    grid_win = {"1.0": {"result": "win", "bars": 2}, "3.0": {"result": "loss", "bars": 5}}
    grid_loss = {"1.0": {"result": "loss", "bars": 1}, "3.0": {"result": "loss", "bars": 1}}
    for i in range(3):
        add(con, i, r_grid=grid_win)
    add(con, 3, r_grid=grid_loss)

    result = run(con)
    by_target = {row["target_r"]: row for row in result["target_grid"]}

    one_r = by_target[1.0]
    assert one_r["wins"] == 3
    assert one_r["decided"] == 4
    assert one_r["win_rate"] == pytest.approx(0.75)
    # Three wins at 1R against one full-risk loss.
    assert one_r["expectancy_r"] == pytest.approx(0.5)

    assert by_target[3.0]["win_rate"] == pytest.approx(0.0)
    # A target nothing reached has no decided trades rather than a zero win rate.
    assert by_target[5.0]["decided"] == 0
    assert by_target[5.0]["win_rate"] is None


def test_excursion_medians_come_back(con):
    for i, (mfe, mae) in enumerate([(1.0, -0.2), (2.0, -0.4), (3.0, -0.6)]):
        add(con, i, mfe_r=mfe, mae_r=mae)

    result = run(con)
    assert result["median_mfe_r"] == pytest.approx(2.0)
    assert result["median_mae_r"] == pytest.approx(-0.4)


def test_blinded_only_narrows_to_blinded_sessions(con):
    for i in range(3):
        add(con, i, blinded=True)
    for i in range(3, 6):
        add(con, i, blinded=False)

    assert run(con)["decided"] == 6
    assert run(con, blinded_only=True)["decided"] == 3


def test_thin_sample_reports_no_signal(con):
    add(con, 0)
    result = run(con, min_samples=5)
    assert result["level_used"] == "no_signal"
    assert result["min_samples_required"] == 5
    assert result["decided_available"] == 1
