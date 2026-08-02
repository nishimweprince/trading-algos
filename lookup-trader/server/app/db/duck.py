from __future__ import annotations

from pathlib import Path

import duckdb

from app.config import settings


def get_connection(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    path = db_path or settings.duckdb_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def register_candles_view(con: duckdb.DuckDBPyConnection) -> None:
    glob = settings.candles_parquet_glob.replace("'", "''")
    con.execute(
        f"""
        CREATE OR REPLACE VIEW candles AS
        SELECT symbol, timeframe, ts, open, high, low, close, volume
        FROM read_parquet('{glob}', hive_partitioning = 1)
        """
    )
