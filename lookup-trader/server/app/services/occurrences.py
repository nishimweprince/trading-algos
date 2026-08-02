from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from app.config import settings
from app.services.candles import fetch_candles
from app.services.context import compute_context
from app.services.labeler import label_triple_barrier
from app.utils.time import to_utc, to_utc_iso


def validate_setup(con: duckdb.DuckDBPyConnection, setup_id: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM setups WHERE setup_id = ? AND active = TRUE",
        [setup_id],
    ).fetchone()
    return row is not None


def insert_occurrence(con: duckdb.DuckDBPyConnection, data: dict) -> dict:
    occ_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO occurrences (
          id, source, session_id, symbol, timeframe, ts, setup_id, side,
          entry, sl, tp, max_bars, atr_period, atr_at_signal,
          result, realized_r, bars_to_resolution, observed_result,
          trend_state, atr_bucket, session, rsi_band,
          calendar_flag, calendar_tags, notes, labeler_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            occ_id,
            data["source"],
            data.get("session_id"),
            data["symbol"],
            data["timeframe"],
            data["ts"],
            data["setup_id"],
            data["side"],
            data["entry"],
            data["sl"],
            data["tp"],
            data["max_bars"],
            data["atr_period"],
            data.get("atr_at_signal"),
            data["result"],
            data.get("realized_r"),
            data.get("bars_to_resolution"),
            data.get("observed_result"),
            data.get("trend_state"),
            data.get("atr_bucket"),
            data.get("session"),
            data.get("rsi_band"),
            data.get("calendar_flag"),
            data.get("calendar_tags"),
            data.get("notes"),
            data.get("labeler_version"),
        ],
    )
    row = con.execute("SELECT * FROM occurrences WHERE id = ?", [occ_id]).fetchdf()
    return _row_to_dict(row.iloc[0])


def _row_to_dict(row) -> dict:
    d = row.to_dict()
    ts_fields = {"ts", "started_at", "ended_at", "date_from", "date_to", "created_at"}
    for k, v in d.items():
        if hasattr(v, "isoformat") and k in ts_fields:
            d[k] = to_utc_iso(v.to_pydatetime() if hasattr(v, "to_pydatetime") else v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif hasattr(v, "item"):
            v = v.item()
            d[k] = None if isinstance(v, float) and math.isnan(v) else v
        elif isinstance(v, float) and math.isnan(v):
            d[k] = None
    if "id" in d:
        d["id"] = str(d["id"])
    if "session_id" in d and d["session_id"] is not None:
        d["session_id"] = str(d["session_id"])
    return d


def list_occurrences(con: duckdb.DuckDBPyConnection, session_id: str | None = None) -> list[dict]:
    if session_id:
        df = con.execute(
            "SELECT * FROM occurrences WHERE session_id = ? ORDER BY created_at DESC",
            [session_id],
        ).fetchdf()
    else:
        df = con.execute("SELECT * FROM occurrences ORDER BY created_at DESC").fetchdf()
    return [_row_to_dict(row) for _, row in df.iterrows()]


def process_trade(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str | None,
    symbol: str,
    timeframe: str,
    signal_ts: datetime,
    setup_id: str,
    side: int,
    entry: float,
    sl: float,
    tp: float,
    notes: str | None = None,
    calendar_flag: bool | None = None,
    calendar_tags: str | None = None,
    observed_result: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict:
    if not validate_setup(con, setup_id):
        raise ValueError(f"Invalid or inactive setup_id: {setup_id}")

    signal_ts = to_utc(signal_ts)
    if date_from is not None:
        date_from = to_utc(date_from)
    else:
        date_from = signal_ts - timedelta(days=30)
    if date_to is not None:
        date_to = to_utc(date_to)
    else:
        date_to = signal_ts + timedelta(days=7)

    # Extend window to include forward bars for labeling
    tf_hours = {"M1": 1 / 60, "M5": 5 / 60, "M15": 0.25, "M30": 0.5, "H1": 1, "H4": 4, "D1": 24}
    hours_per_bar = tf_hours.get(timeframe.upper(), 1)
    date_to = max(date_to, signal_ts + timedelta(hours=settings.max_bars * hours_per_bar * 2))

    candles_df = fetch_candles(con, symbol, timeframe, date_from, date_to)
    if candles_df.empty:
        raise ValueError("No candles found for the given window")

    candles_df = candles_df.reset_index(drop=True)
    signal_idx = None
    signal_utc = to_utc(signal_ts)
    for i, ts in enumerate(candles_df["ts"]):
        bar_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if to_utc(bar_ts) == signal_utc:
            signal_idx = i
            break

    if signal_idx is None:
        raise ValueError(f"signal_ts {signal_ts} not found in candle window")

    ctx = compute_context(candles_df, signal_idx)
    label = label_triple_barrier(
        candles_df,
        signal_idx=signal_idx,
        side=side,
        entry=entry,
        sl=sl,
        tp=tp,
        max_bars=settings.max_bars,
        ambiguous=settings.ambiguous_policy,
    )

    return insert_occurrence(
        con,
        {
            "source": "manual",
            "session_id": session_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": signal_ts,
            "setup_id": setup_id,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "max_bars": settings.max_bars,
            "atr_period": settings.atr_period,
            "atr_at_signal": ctx["atr_at_signal"],
            "result": label["result"],
            "realized_r": label["realized_r"],
            "bars_to_resolution": label["bars_to_resolution"],
            "observed_result": observed_result,
            "trend_state": ctx["trend_state"],
            "atr_bucket": ctx["atr_bucket"],
            "session": ctx["session"],
            "rsi_band": ctx["rsi_band"],
            "calendar_flag": calendar_flag,
            "calendar_tags": calendar_tags,
            "notes": notes,
            "labeler_version": settings.labeler_version,
        },
    )
