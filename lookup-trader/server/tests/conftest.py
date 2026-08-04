"""Run the suite against a scratch DuckDB, not the operator's real one.

Two reasons: the API tests insert occurrences, which would otherwise land in the
labelled dataset alongside real work; and DuckDB takes an exclusive file lock, so
a running dev server would make the whole suite fail.

The env var has to be set before app.config is imported anywhere — hence the
assignment above the imports.
"""

import os
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / "lookup_trader_test.duckdb"
os.environ.setdefault("LOOKUP_DUCKDB_PATH", str(_TEST_DB))

import pytest  # noqa: E402

from app.db.bootstrap import bootstrap  # noqa: E402
from app.db.duck import close_all  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_db():
    # Start from a clean schema so migrations are exercised on a fresh database.
    close_all()
    _TEST_DB.unlink(missing_ok=True)
    Path(f"{_TEST_DB}.wal").unlink(missing_ok=True)
    bootstrap()
    yield
    close_all()
