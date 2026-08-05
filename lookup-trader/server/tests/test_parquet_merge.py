"""Merge-on-write has to actually overwrite.

Both the candle ingest and the feature builder re-run over months they have
already written, and both rely on a re-run replacing a row rather than racing it.
A merge that keeps the older row is not a visible failure — it is a rewrite that
silently did nothing, which is how a bar rebuilt because its forward window had
elapsed can stay marked incomplete forever.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.utils.parquet import write_month_partition


def _frame(times: pd.DatetimeIndex, marker: str, close: float) -> pd.DataFrame:
    return pd.DataFrame({"ts": times, "marker": marker, "close": close, "complete": True})


def test_a_rewrite_replaces_every_row(tmp_path):
    """Enough rows that a non-stable sort is near-certain to reorder some.

    At 1500 rows the old sort-then-deduplicate order left about half the frame at
    its previous values, so a handful of rows is not a sufficient test.
    """
    times = pd.date_range("2026-03-30", periods=1500, freq="h", tz="UTC")
    out = tmp_path / "part-000.parquet"

    write_month_partition(out, _frame(times, "old", 1.0))
    write_month_partition(out, _frame(times, "new", 2.0))

    written = pd.read_parquet(out)
    assert len(written) == 1500
    assert written["marker"].unique().tolist() == ["new"]
    assert written["close"].unique().tolist() == pytest.approx([2.0])


def test_a_partial_rewrite_leaves_untouched_rows_alone(tmp_path):
    """A re-run over part of a month must not drop the rest of it."""
    times = pd.date_range("2026-03-30", periods=100, freq="h", tz="UTC")
    out = tmp_path / "part-000.parquet"

    write_month_partition(out, _frame(times, "old", 1.0))
    write_month_partition(out, _frame(times[60:], "new", 2.0))

    written = pd.read_parquet(out).sort_values("ts").reset_index(drop=True)
    assert len(written) == 100
    assert written["marker"].tolist() == ["old"] * 60 + ["new"] * 40


def test_new_rows_are_appended(tmp_path):
    times = pd.date_range("2026-03-30", periods=100, freq="h", tz="UTC")
    out = tmp_path / "part-000.parquet"

    write_month_partition(out, _frame(times[:50], "old", 1.0))
    write_month_partition(out, _frame(times[50:], "new", 2.0))

    written = pd.read_parquet(out)
    assert len(written) == 100
    assert written["ts"].is_unique


def test_the_result_stays_sorted_by_key(tmp_path):
    """Readers and the chart both assume ascending time."""
    times = pd.date_range("2026-03-30", periods=500, freq="h", tz="UTC")
    out = tmp_path / "part-000.parquet"

    write_month_partition(out, _frame(times[250:], "a", 1.0))
    write_month_partition(out, _frame(times[:250], "b", 2.0))

    written = pd.read_parquet(out)
    assert written["ts"].is_monotonic_increasing


def test_a_rewrite_can_flip_a_bar_to_complete(tmp_path):
    """The deferred-resolution path: a bar written before its forward window had
    elapsed is rebuilt later, and the later row is the one that counts."""
    times = pd.date_range("2026-03-30", periods=800, freq="h", tz="UTC")
    out = tmp_path / "part-000.parquet"

    pending = _frame(times, "old", 1.0)
    pending["complete"] = False
    write_month_partition(out, pending)
    write_month_partition(out, _frame(times, "new", 2.0))

    written = pd.read_parquet(out)
    assert written["complete"].all()
