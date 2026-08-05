"""Serving a bar's pattern tags: the stored row when there is one, live when not.

Both branches end at the same `tag_bar`. The store branch deserializes what the
builder wrote; the live branch recomputes it now. There is no second
implementation of the rules, so the only thing the two can disagree about is
staleness — and the version predicate turns staleness into a fallback rather than
a wrong answer.

Falling back matters more than it looks. Only one symbol has ever been built, and
the store starts 200 bars into history, so the live path is not an edge case — it
is what most `/context` calls actually take.
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb
import pandas as pd

from app.config import settings
from app.services.base_rate import store_is_built
from app.taggers import TagResult, tag_bar

STORE = "store"
LIVE = "live"


def store_has_tag_columns(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether the view exposes `bar_tags`.

    `store_is_built` only proves a store exists — not that it was built by a
    version that knew about tagging.
    """
    row = con.execute(
        "SELECT count(*) FROM duckdb_columns() "
        "WHERE table_name = 'bar_features' AND column_name = 'bar_tags'"
    ).fetchone()
    return bool(row and row[0])


def primary_or_none(tags: TagResult) -> str | None:
    """The primary setup id, with the store's empty-string sentinel as None.

    The stored column cannot be NULL — an all-null string column in a quiet month
    writes a null-typed Parquet column — so absence is spelled "" on disk and
    None everywhere above it. This is the one place that translates.
    """
    return tags.primary_setup_id() or None


def _from_store(
    con: duckdb.DuckDBPyConnection, symbol: str, timeframe: str, ts: datetime
) -> TagResult | None:
    """The stored tags for one bar, or None if there is nothing usable there."""
    try:
        row = con.execute(
            """
            SELECT bar_tags
            FROM bar_features
            WHERE symbol = ? AND timeframe = ? AND ts = ?
              AND bar_feature_version = ?
            """,
            [symbol, timeframe, ts, settings.bar_feature_version],
        ).fetchone()
    except duckdb.Error:
        return None

    if not row or not row[0]:
        return None
    try:
        return TagResult.from_json(json.loads(row[0]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def resolve_bar_tags(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    ts: datetime,
    window: pd.DataFrame,
    atr_at_bar: float | None,
) -> tuple[TagResult, str]:
    """Tags for one bar, and which path produced them.

    The source is returned rather than hidden because it is the first thing worth
    knowing when the tags look wrong: stale store, or a symbol that was never
    built. It is derived from bars at or before the anchor either way, so it
    reveals nothing the caller could not already see.
    """
    if store_is_built(con) and store_has_tag_columns(con):
        stored = _from_store(con, symbol, timeframe, ts)
        if stored is not None:
            return stored, STORE
    return tag_bar(window, atr_at_bar), LIVE
