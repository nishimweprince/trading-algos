from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from app.config import settings

# DuckDB takes an exclusive lock on the database file, so one instance per
# process — not one per request. Requests get a cursor into that shared
# instance; without this, the three calls a page load fires in parallel race
# each other for the lock and the losers fail with an IO error.
_lock = threading.Lock()
_instances: dict[str, duckdb.DuckDBPyConnection] = {}


def _instance(path: Path) -> duckdb.DuckDBPyConnection:
    key = str(path)
    with _lock:
        con = _instances.get(key)
        if con is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(key)
            _instances[key] = con
        return con


def get_connection(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """A cursor into the process-wide instance.

    Callers close it as before; closing a cursor leaves the instance open.
    """
    return _instance(db_path or settings.duckdb_path).cursor()


def close_all() -> None:
    """Drop the cached instances, releasing the file locks."""
    with _lock:
        for con in _instances.values():
            con.close()
        _instances.clear()
        _view_ready.clear()


_view_ready: set[str] = set()


def register_candles_view(con: duckdb.DuckDBPyConnection, force: bool = False) -> None:
    """Ensure the candles view exists, without writing the catalog every request.

    CREATE OR REPLACE VIEW is a catalog write, and the routers call this on every
    request — run unconditionally, two concurrent requests abort with a
    write-write conflict. The view is persisted in the database, so once it is
    there the check is all that is needed.
    """
    key = str(settings.duckdb_path)
    if not force and key in _view_ready:
        return

    with _lock:
        if not force and key in _view_ready:
            return
        exists = con.execute(
            "SELECT 1 FROM duckdb_views() WHERE view_name = 'candles'"
        ).fetchone()
        if force or exists is None:
            glob = settings.candles_parquet_glob.replace("'", "''")
            con.execute(
                f"""
                CREATE OR REPLACE VIEW candles AS
                SELECT symbol, timeframe, ts, open, high, low, close, volume
                FROM read_parquet('{glob}', hive_partitioning = 1)
                """
            )
        _view_ready.add(key)
