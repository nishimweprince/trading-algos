"""`/context` serves stored tags when it can and recomputes them when it cannot.

The fallback is the part that needs the coverage. Only one symbol has ever been
built and the store starts 200 bars into history, so a fallback that quietly
returned no tags would look correct in every integration test and broken on most
real bars.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pandas as pd
import pytest

from app.config import settings
from app.services.bar_tags import (
    LIVE,
    STORE,
    primary_or_none,
    resolve_bar_tags,
    store_has_tag_columns,
)
from app.taggers import TagResult, tag_bar

TS = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)

# A bullish engulfing, from the golden corpus.
BARS = [
    [100.0, 100.5, 99.0, 99.2],
    [99.1, 101.0, 99.0, 100.3],
]
ATR = 1.0


def _window() -> pd.DataFrame:
    frame = pd.DataFrame(BARS, columns=["open", "high", "low", "close"])
    frame["ts"] = pd.date_range(TS - pd.Timedelta(1, unit="h"), periods=len(frame), freq="h")
    frame["volume"] = 1000.0
    return frame[["ts", "open", "high", "low", "close", "volume"]]


def _con(*, tag_columns: bool = True, level_touch: bool = True) -> duckdb.DuckDBPyConnection:
    """A minimal bar_features table.

    Deliberately its own helper rather than an extension of test_base_rate's:
    that one's `_insert` is imported by another module, so widening it would
    ripple through tests this feature does not touch.
    """
    con = duckdb.connect(":memory:")
    columns = ["symbol VARCHAR", "timeframe VARCHAR", "ts TIMESTAMPTZ", "bar_feature_version VARCHAR"]
    if level_touch:
        # What `store_is_built` probes for.
        columns.append("level_touch VARCHAR")
    if tag_columns:
        columns += ["bar_tags VARCHAR", "tag_primary_setup_id VARCHAR"]
    con.execute(f"CREATE TABLE bar_features ({', '.join(columns)})")
    return con


def _insert(con, payload: str | None, *, version: str | None = None) -> None:
    con.execute(
        "INSERT INTO bar_features VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            "XAUUSD",
            "H1",
            TS,
            version or settings.bar_feature_version,
            "{}",
            payload,
            "bull_engulfing",
        ],
    )


def _resolve(con):
    return resolve_bar_tags(con, "XAUUSD", "H1", TS, window=_window(), atr_at_bar=ATR)


def _stored_payload() -> str:
    """Exactly what the builder writes for this bar."""
    return json.dumps(tag_bar(_window(), ATR).to_json(), separators=(",", ":"))


def test_the_fixture_bar_actually_carries_a_tag() -> None:
    # Otherwise every assertion below would pass on an empty result.
    assert tag_bar(_window(), ATR).setup_ids() == ["bull_engulfing"]


def test_a_stored_row_is_served_from_the_store() -> None:
    con = _con()
    _insert(con, _stored_payload())

    tags, source = _resolve(con)
    assert source == STORE
    assert tags.setup_ids() == ["bull_engulfing"]


def test_stored_and_live_tags_are_identical() -> None:
    """The two paths share one implementation; only staleness may differ."""
    con = _con()
    _insert(con, _stored_payload())
    stored, stored_source = _resolve(con)

    empty = _con()
    live, live_source = _resolve(empty)

    assert (stored_source, live_source) == (STORE, LIVE)
    assert stored.to_json() == live.to_json()


@pytest.mark.parametrize(
    "name,setup",
    [
        ("no store at all", lambda: _con(level_touch=False)),
        ("store built before tagging existed", lambda: _con(tag_columns=False)),
        ("no row for this bar", _con),
    ],
)
def test_missing_store_data_falls_back_to_live(name: str, setup) -> None:
    tags, source = _resolve(setup())
    assert source == LIVE, name
    assert tags.setup_ids() == ["bull_engulfing"], name


@pytest.mark.parametrize("payload", [None, "", "not json", '{"tags": [{"no_setup_id": 1}]}'])
def test_unusable_stored_payloads_fall_back_to_live(payload) -> None:
    con = _con()
    _insert(con, payload)

    tags, source = _resolve(con)
    assert source == LIVE
    assert tags.setup_ids() == ["bull_engulfing"]


def test_a_stale_version_falls_back_rather_than_serving_old_tags() -> None:
    """Tags from an older tagger describe a different vocabulary, not a smaller one."""
    con = _con()
    _insert(con, json.dumps({"version": "0.9.0", "tags": []}), version="0.9.0")

    tags, source = _resolve(con)
    assert source == LIVE
    assert tags.setup_ids() == ["bull_engulfing"]


def test_an_untagged_stored_bar_is_served_as_empty_not_recomputed() -> None:
    """A bar the builder found nothing on is an answer, not a missing row."""
    con = _con()
    _insert(con, json.dumps(TagResult.empty(settings.bar_feature_version).to_json()))

    tags, source = _resolve(con)
    assert source == STORE
    assert tags.tags == ()


def test_store_has_tag_columns_detects_a_pre_tagging_store() -> None:
    assert store_has_tag_columns(_con()) is True
    assert store_has_tag_columns(_con(tag_columns=False)) is False


def test_the_empty_primary_sentinel_becomes_none() -> None:
    assert primary_or_none(TagResult.empty("1.1.0")) is None
    assert primary_or_none(tag_bar(_window(), ATR)) == "bull_engulfing"
