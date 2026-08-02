from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd

from app.config import settings


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


def candles_to_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        ts = row["ts"]
        if hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
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
