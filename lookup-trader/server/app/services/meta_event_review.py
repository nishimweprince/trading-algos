"""Causal-first access to immutable events and separate mutable reviews."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb
import pandas as pd

from app.services.candles import candles_to_records, fetch_labeling_window
from app.services.meta_events import META_MODEL_FEATURES, event_path
from app.utils.time import to_utc_iso

SORT_COLUMNS = {
    "signal_ts": "e.signal_ts",
    "confidence": "e.confidence",
}


def _events_relation(con: duckdb.DuckDBPyConnection) -> str | None:
    path = event_path()
    if not path.exists():
        return None
    escaped = str(path).replace("'", "''")
    return f"read_parquet('{escaped}')"


def _record(row: tuple, columns: list[str]) -> dict[str, Any]:
    result = dict(zip(columns, row, strict=True))
    for key, value in list(result.items()):
        if isinstance(value, (datetime, pd.Timestamp)):
            normalized = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
            result[key] = to_utc_iso(normalized)
    if isinstance(result.get("setup_ids"), str):
        result["setup_ids"] = json.loads(result["setup_ids"])
    return result


def list_events(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    timeframe: str,
    offset: int,
    limit: int,
    year: int | None = None,
    setup: str | None = None,
    side: int | None = None,
    confidence_min: float | None = None,
    quality: str | None = None,
    review_status: str | None = None,
    sort: str = "signal_ts",
    order: str = "desc",
) -> dict[str, Any]:
    relation = _events_relation(con)
    if relation is None:
        return {"items": [], "offset": offset, "limit": limit, "total": 0, "has_next": False}
    conditions, params = ["e.symbol = ?", "e.timeframe = ?"], [symbol.upper(), timeframe.upper()]
    if year is not None:
        conditions.append("year(e.signal_ts) = ?")
        params.append(year)
    if setup:
        conditions.append("e.primary_setup_id = ?")
        params.append(setup)
    if side is not None:
        conditions.append("e.side = ?")
        params.append(side)
    if confidence_min is not None:
        conditions.append("e.confidence >= ?")
        params.append(confidence_min)
    if quality == "reliable":
        conditions.append("e.data_quality_reliable IS TRUE AND e.context_reliable IS TRUE")
    elif quality == "unreliable":
        conditions.append("NOT (e.data_quality_reliable IS TRUE AND e.context_reliable IS TRUE)")
    if review_status == "unreviewed":
        conditions.append("r.verdict IS NULL")
    elif review_status in {"valid", "invalid", "uncertain"}:
        conditions.append("r.verdict = ?")
        params.append(review_status)
    elif review_status == "revealed":
        conditions.append("r.revealed_at IS NOT NULL")
    where = " AND ".join(conditions)
    sort_column = SORT_COLUMNS.get(sort, SORT_COLUMNS["signal_ts"])
    sort_direction = "DESC" if order == "desc" else "ASC"
    order_clause = f"{sort_column} {sort_direction}, e.side, e.event_id"
    total = con.execute(
        f"SELECT count(*) FROM {relation} e "
        f"LEFT JOIN meta_event_reviews r USING(event_id) WHERE {where}",
        params,
    ).fetchone()[0]
    columns = [
        "event_id", "symbol", "timeframe", "signal_ts", "side", "primary_setup_id",
        "setup_ids", "confidence", "tag_source", "data_quality_reliable",
        "context_reliable", "verdict", "reviewed_at", "revealed_at",
    ]
    rows = con.execute(
        f"""
        SELECT e.event_id, e.symbol, e.timeframe, e.signal_ts, e.side,
               e.primary_setup_id, e.setup_ids, e.confidence, e.tag_source,
               e.data_quality_reliable, e.context_reliable,
               r.verdict, r.reviewed_at, r.revealed_at
        FROM {relation} e LEFT JOIN meta_event_reviews r USING(event_id)
        WHERE {where} ORDER BY {order_clause} LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return {
        "items": [_record(row, columns) for row in rows],
        "offset": offset,
        "limit": limit,
        "total": int(total),
        "has_next": offset + len(rows) < total,
    }


def summary(con: duckdb.DuckDBPyConnection, symbol: str, timeframe: str) -> dict[str, Any]:
    relation = _events_relation(con)
    if relation is None:
        return {
            "total": 0,
            "by_year": {},
            "by_setup": {},
            "by_side": {},
            "by_quality": {},
            "by_review": {},
        }
    params = [symbol.upper(), timeframe.upper()]
    where = "e.symbol = ? AND e.timeframe = ?"

    def grouped(expr: str) -> dict[str, int]:
        query = (
            f"SELECT {expr}, count(*) FROM {relation} e "
            f"LEFT JOIN meta_event_reviews r USING(event_id) "
            f"WHERE {where} GROUP BY 1 ORDER BY 1"
        )
        return {
            str(key): int(value)
            for key, value in con.execute(query, params).fetchall()
        }

    total_query = f"SELECT count(*) FROM {relation} e WHERE e.symbol = ? AND e.timeframe = ?"
    total = con.execute(total_query, params).fetchone()[0]
    return {
        "total": int(total),
        "by_year": grouped("year(e.signal_ts)"),
        "by_setup": grouped("e.primary_setup_id"),
        "by_side": grouped("e.side"),
        "by_quality": grouped(
            "CASE WHEN e.data_quality_reliable AND e.context_reliable "
            "THEN 'reliable' ELSE 'unreliable' END"
        ),
        "by_review": grouped("coalesce(r.verdict, 'unreviewed')"),
    }


def causal_detail(con: duckdb.DuckDBPyConnection, event_id: str) -> dict[str, Any] | None:
    relation = _events_relation(con)
    if relation is None:
        return None
    causal = [
        "event_id", "symbol", "timeframe", "signal_ts", "side", "primary_setup_id",
        "setup_ids", "confidence", "tag_source", "tagger_model_version",
        "data_quality_reliable", "context_reliable", "bar_feature_version",
        "meta_feature_version", "meta_label_version", "signal_close", "atr_at_signal",
        *META_MODEL_FEATURES,
    ]
    # De-duplicate fields shared by the metadata and model lists.
    causal = list(dict.fromkeys(causal))
    quoted = ", ".join(f'e."{name}"' for name in causal)
    row = con.execute(
        f"SELECT {quoted}, r.verdict, r.pre_notes, r.reviewed_at, r.revealed_at, r.post_notes "
        f"FROM {relation} e LEFT JOIN meta_event_reviews r USING(event_id) WHERE e.event_id = ?",
        [event_id],
    ).fetchone()
    if row is None:
        return None
    columns = [*causal, "verdict", "pre_notes", "reviewed_at", "revealed_at", "post_notes"]
    result = _record(row, columns)
    signal_ts = pd.Timestamp(result["signal_ts"]).to_pydatetime()
    candles, _ = fetch_labeling_window(
        con, result["symbol"], result["timeframe"], signal_ts, warmup_bars=301, forward_bars=0
    )
    result["history_candles"] = candles_to_records(candles)
    return result


def save_review(
    con: duckdb.DuckDBPyConnection,
    event_id: str,
    *,
    verdict: str | None,
    notes: str | None,
    phase: str,
) -> dict[str, Any]:
    if causal_detail(con, event_id) is None:
        raise KeyError(event_id)
    existing = con.execute(
        "SELECT verdict, revealed_at FROM meta_event_reviews WHERE event_id = ?", [event_id]
    ).fetchone()
    if phase == "pre":
        if existing and existing[1] is not None:
            raise RuntimeError("The outcome has already been revealed; pre-reveal review is frozen")
        con.execute(
            """
            INSERT INTO meta_event_reviews(event_id, verdict, pre_notes, reviewed_at, updated_at)
            VALUES (?, ?, ?, now(), now())
            ON CONFLICT(event_id) DO UPDATE SET verdict=excluded.verdict,
              pre_notes=excluded.pre_notes, reviewed_at=excluded.reviewed_at, updated_at=now()
            """,
            [event_id, verdict, notes],
        )
    else:
        if not existing or existing[1] is None:
            raise RuntimeError("Reveal the outcome before saving post-reveal notes")
        con.execute(
            "UPDATE meta_event_reviews SET post_notes = ?, updated_at = now() WHERE event_id = ?",
            [notes, event_id],
        )
    return causal_detail(con, event_id) or {}


def reveal(con: duckdb.DuckDBPyConnection, event_id: str) -> dict[str, Any]:
    relation = _events_relation(con)
    if relation is None:
        raise KeyError(event_id)
    review = con.execute(
        "SELECT verdict FROM meta_event_reviews WHERE event_id = ?", [event_id]
    ).fetchone()
    if not review or review[0] not in {"valid", "invalid", "uncertain"}:
        raise PermissionError("Record a detector-validity review before revealing the outcome")
    fields = [
        "event_id", "entry_ts", "exit_ts", "entry_price", "stop_price", "target_price",
        "exit_price", "outcome", "gross_r", "bars_to_resolution", "ambiguous_bar",
        "cost_r_3", "net_r_3", "y_meta_3", "cost_r_5", "net_r_5", "y_meta_5",
        "cost_r_8", "net_r_8", "y_meta_8", "y_meta", "symbol", "timeframe", "signal_ts",
    ]
    row = con.execute(
        f"SELECT {', '.join(fields)} FROM {relation} WHERE event_id = ?", [event_id]
    ).fetchone()
    if row is None:
        raise KeyError(event_id)
    con.execute(
        "UPDATE meta_event_reviews SET revealed_at = coalesce(revealed_at, now()), "
        "updated_at = now() WHERE event_id = ?",
        [event_id],
    )
    result = _record(row, fields)
    candles, _ = fetch_labeling_window(
        con,
        result["symbol"],
        result["timeframe"],
        pd.Timestamp(result["signal_ts"]).to_pydatetime(),
        warmup_bars=1,
        forward_bars=24,
    )
    result["forward_candles"] = candles_to_records(candles.iloc[1:])
    result["revealed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return result
