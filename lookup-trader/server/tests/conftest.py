"""Run the suite against a scratch DuckDB, not the operator's real one.

Two reasons: the API tests insert occurrences, which would otherwise land in the
labelled dataset alongside real work; and DuckDB takes an exclusive file lock, so
a running dev server would make the whole suite fail.

The env var has to be set before app.config is imported anywhere — hence the
assignment above the imports.
"""

import os
import shutil
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / "lookup_trader_test.duckdb"
_TEST_DATA = Path(tempfile.gettempdir()) / "lookup_trader_test_data"
os.environ.setdefault("LOOKUP_DUCKDB_PATH", str(_TEST_DB))
os.environ.setdefault("LOOKUP_DATA_DIR", str(_TEST_DATA))
os.environ.setdefault("LOOKUP_SHADOW_DB_PATH", str(_TEST_DATA / "shadow.sqlite3"))

import pytest  # noqa: E402
import pandas as pd  # noqa: E402

from app.db.bootstrap import bootstrap  # noqa: E402
from app.db.duck import close_all  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_db():
    # Start from a clean schema so migrations are exercised on a fresh database.
    close_all()
    _TEST_DB.unlink(missing_ok=True)
    Path(f"{_TEST_DB}.wal").unlink(missing_ok=True)
    shutil.rmtree(_TEST_DATA, ignore_errors=True)
    fixtures = (
        ("EURUSD", 1.1, "2024-01-01T01:00:00Z", 96, 0.0001),
        ("XAUUSD", 4_260.0, "2026-01-01T01:00:00Z", 420, 0.2),
    )
    for symbol, base, start, periods, step in fixtures:
        ts = pd.date_range(start, periods=periods, freq="h")
        close = [base + i * step for i in range(len(ts))]
        frame = pd.DataFrame(
            {
                "ts": ts,
                "open": close,
                "high": [value + (0.001 if symbol == "EURUSD" else 1.0) for value in close],
                "low": [value - (0.001 if symbol == "EURUSD" else 1.0) for value in close],
                "close": close,
                "volume": [100.0] * len(ts),
            }
        )
        path = (
            _TEST_DATA / "candles" / f"symbol={symbol}" / "timeframe=H1"
            / f"year={ts[0].year}" / f"month={ts[0].month:02d}" / "part-000.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    bootstrap()
    yield
    close_all()
