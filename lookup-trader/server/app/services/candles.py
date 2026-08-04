from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd

from app.config import settings
from app.utils.time import to_utc, to_utc_iso


def fetch_symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    try:
        rows = con.execute("SELECT DISTINCT symbol FROM candles ORDER BY symbol").fetchall()
        return [r[0] for r in rows]
    except duckdb.CatalogException:
        return []


def fetch_timeframes(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    try:
        rows = con.execute(
            "SELECT DISTINCT timeframe FROM candles WHERE symbol = ? ORDER BY timeframe",
            [symbol],
        ).fetchall()
        return [r[0] for r in rows]
    except duckdb.CatalogException:
        return []


def fetch_candles(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    date_from: datetime,
    date_to: datetime,
) -> pd.DataFrame:
    date_from = to_utc(date_from)
    date_to = to_utc(date_to)
    df = con.execute(
        """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND timeframe = ?
          AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
        """,
        [symbol, timeframe, date_from, date_to],
    ).df()
    return df


def fetch_labeling_window(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    signal_ts: datetime,
    warmup_bars: int,
    forward_bars: int,
) -> tuple[pd.DataFrame, int]:
    """Fetch a fixed bar count around the signal, returning (candles, signal_idx).

    Deliberately bar-count based rather than date based: the operator's session
    window must not influence the indicator warmup, or the same bar labelled in
    two different sessions gets different context features.
    """
    signal_ts = to_utc(signal_ts)

    history = con.execute(
        """
        SELECT ts, open, high, low, close, volume FROM (
          SELECT ts, open, high, low, close, volume
          FROM candles
          WHERE symbol = ? AND timeframe = ? AND ts <= ?
          ORDER BY ts DESC
          LIMIT ?
        ) ORDER BY ts ASC
        """,
        [symbol, timeframe, signal_ts, warmup_bars],
    ).df()

    forward = con.execute(
        """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND timeframe = ? AND ts > ?
        ORDER BY ts ASC
        LIMIT ?
        """,
        [symbol, timeframe, signal_ts, forward_bars],
    ).df()

    if history.empty:
        raise ValueError(f"No candles at or before {to_utc_iso(signal_ts)} for {symbol} {timeframe}")

    signal_idx = len(history) - 1
    df = pd.concat([history, forward], ignore_index=True)

    bar_ts = df.iloc[signal_idx]["ts"]
    bar_ts = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
    if to_utc(bar_ts) != signal_ts:
        raise ValueError(f"signal_ts {to_utc_iso(signal_ts)} is not a bar in {symbol} {timeframe}")

    return df, signal_idx


def candles_to_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        ts = row["ts"]
        if hasattr(ts, "to_pydatetime"):
            ts_str = to_utc_iso(ts.to_pydatetime())
        elif hasattr(ts, "isoformat"):
            ts_str = to_utc_iso(ts)
        else:
            ts_str = str(ts)
        records.append(
            {
                "ts": ts_str,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]) if row["volume"] is not None else 0.0,
            }
        )
    return records
