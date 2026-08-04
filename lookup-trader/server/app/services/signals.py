from __future__ import annotations

import json
import uuid
from datetime import datetime

import duckdb

from app.config import settings
from app.services.candles import fetch_labeling_window
from app.services.compare import compare_occurrences
from app.services.context import compute_context, compute_htf_trend_state, context_fingerprint
from app.utils.time import to_utc, to_utc_iso

SIGNAL_COLUMNS = (
    "id, source, session_id, symbol, timeframe, ts, setup_id, side, "
    "cursor_idx, bars_visible, peeked, blinded, feature_version, "
    "context_snapshot, compare_context, compare_at_signal, context_fingerprint, "
    "screenshot_signal, chart_render_spec, confluence_tags, calendar_flag, "
    "calendar_tags, confidence, at_key_level, level_type, consolidation_before, "
    "annotation_sources, lifecycle, occurrence_id"
).split(", ")

JSON_COLUMNS = (
    "context_snapshot",
    "compare_context",
    "compare_at_signal",
    "chart_render_spec",
    "annotation_sources",
)
TS_COLUMNS = {"ts", "created_at"}


def _row_to_dict(row) -> dict:
    d = row.to_dict()
    for k, v in d.items():
        if k in JSON_COLUMNS and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                d[k] = v
        elif hasattr(v, "isoformat") and k in TS_COLUMNS:
            d[k] = to_utc_iso(v.to_pydatetime() if hasattr(v, "to_pydatetime") else v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif hasattr(v, "item"):
            v = v.item()
            d[k] = None if isinstance(v, float) and v != v else v
        elif isinstance(v, float) and v != v:
            d[k] = None
    if "id" in d:
        d["id"] = str(d["id"])
    if "session_id" in d and d["session_id"] is not None:
        d["session_id"] = str(d["session_id"])
    if "occurrence_id" in d and d["occurrence_id"] is not None:
        d["occurrence_id"] = str(d["occurrence_id"])
    return d


def insert_signal(con: duckdb.DuckDBPyConnection, data: dict) -> dict:
    signal_id = str(uuid.uuid4())
    row = {"id": signal_id}
    for column in SIGNAL_COLUMNS:
        if column == "id" or column not in data:
            continue
        value = data[column]
        if column in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        row[column] = value

    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    con.execute(
        f"INSERT INTO signals ({columns}) VALUES ({placeholders})",
        list(row.values()),
    )
    written = con.execute("SELECT * FROM signals WHERE id = ?", [signal_id]).fetchdf()
    return _row_to_dict(written.iloc[0])


def get_signal(con: duckdb.DuckDBPyConnection, signal_id: str) -> dict | None:
    df = con.execute("SELECT * FROM signals WHERE id = ?", [signal_id]).fetchdf()
    if df.empty:
        return None
    return _row_to_dict(df.iloc[0])


def link_signal_to_occurrence(
    con: duckdb.DuckDBPyConnection, signal_id: str, occurrence_id: str
) -> None:
    con.execute(
        "UPDATE signals SET lifecycle = 'linked', occurrence_id = ? WHERE id = ?",
        [occurrence_id, signal_id],
    )


def process_signal(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str | None,
    symbol: str,
    timeframe: str,
    signal_ts: datetime,
    setup_id: str | None = None,
    side: int | None = None,
    cursor_idx: int | None = None,
    bars_visible: int | None = None,
    peeked: bool | None = None,
    blinded: bool | None = None,
    confluence_tags: str | None = None,
    calendar_flag: bool | None = None,
    calendar_tags: str | None = None,
    confidence: int | None = None,
    at_key_level: bool | None = None,
    level_type: str | None = None,
    consolidation_before: bool | None = None,
    annotation_sources: dict | None = None,
    compare_context: dict | None = None,
    compare_pinned: list[str] | None = None,
    compare_min_samples: int | None = None,
    compare_source: str = "manual",
    screenshot_signal: str | None = None,
    chart_render_spec: dict | None = None,
    source: str = "manual",
) -> dict:
    """Persist a causal snapshot at signal-bar close for later comparison audit."""
    signal_ts = to_utc(signal_ts)
    candles_df, signal_idx = fetch_labeling_window(
        con,
        symbol,
        timeframe,
        signal_ts,
        warmup_bars=settings.warmup_bars,
        forward_bars=0,
    )

    htf_trend = compute_htf_trend_state(con, symbol, timeframe, signal_ts)
    ctx = compute_context(candles_df, signal_idx, htf_trend_state=htf_trend)

    compare_result = None
    if setup_id and compare_context is not None:
        compare_result = compare_occurrences(
            con,
            setup_id=setup_id,
            symbol=symbol,
            timeframe=timeframe,
            context=compare_context,
            source=compare_source,
            min_samples=compare_min_samples,
            pinned=compare_pinned or [],
        )

    fingerprint = context_fingerprint(setup_id, symbol, timeframe, ctx)

    row = {
        "source": source,
        "session_id": session_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": signal_ts,
        "setup_id": setup_id,
        "side": side,
        "cursor_idx": cursor_idx,
        "bars_visible": bars_visible,
        "peeked": peeked,
        "blinded": blinded,
        "feature_version": settings.feature_version,
        "context_snapshot": ctx,
        "compare_context": compare_context,
        "compare_at_signal": compare_result,
        "context_fingerprint": fingerprint,
        "screenshot_signal": screenshot_signal,
        "chart_render_spec": chart_render_spec,
        "confluence_tags": confluence_tags,
        "calendar_flag": calendar_flag,
        "calendar_tags": calendar_tags,
        "confidence": confidence,
        "at_key_level": at_key_level,
        "level_type": level_type,
        "consolidation_before": consolidation_before,
        "annotation_sources": annotation_sources,
        "lifecycle": "open",
    }
    return insert_signal(con, row)


def list_signals(con: duckdb.DuckDBPyConnection, session_id: str | None = None) -> list[dict]:
    if session_id:
        df = con.execute(
            "SELECT * FROM signals WHERE session_id = ? ORDER BY created_at DESC",
            [session_id],
        ).fetchdf()
    else:
        df = con.execute("SELECT * FROM signals ORDER BY created_at DESC").fetchdf()
    return [_row_to_dict(row) for _, row in df.iterrows()]
