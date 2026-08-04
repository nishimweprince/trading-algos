from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.db.duck import _candles_view_query, register_candles_view


def test_candles_view_unions_legacy_and_month_partitions(tmp_path):
    root = tmp_path / "candles" / "symbol=EURUSD" / "timeframe=H1" / "year=2024"
    legacy = root / "part-000.parquet"
    month_dir = root / "month=02"
    month_dir.mkdir(parents=True)
    month_file = month_dir / "part-000.parquet"

    legacy_df = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2024-01-02 00:00:00"], utc=True),
            "open": [1.1],
            "high": [1.2],
            "low": [1.0],
            "close": [1.15],
            "volume": [100.0],
        }
    )
    month_df = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2024-02-02 00:00:00"], utc=True),
            "open": [1.2],
            "high": [1.3],
            "low": [1.1],
            "close": [1.25],
            "volume": [110.0],
        }
    )
    legacy_df.to_parquet(legacy, index=False)
    month_df.to_parquet(month_file, index=False)

    con = duckdb.connect()
    con.execute(_candles_view_query(tmp_path))
    rows = con.execute("SELECT ts, close FROM candles ORDER BY ts").fetchall()
    assert len(rows) == 2
    assert rows[0][1] == pytest.approx(1.15)
    assert rows[1][1] == pytest.approx(1.25)


def test_register_candles_view_on_empty_dir(tmp_path, monkeypatch):
    import app.config as config_mod
    import app.db.duck as duck_mod

    monkeypatch.setattr(config_mod.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config_mod.settings, "duckdb_path", tmp_path / "test.duckdb")
    duck_mod._view_ready.clear()

    con = duckdb.connect()
    register_candles_view(con, force=True)
    count = con.execute("SELECT count(*) FROM candles").fetchone()[0]
    assert count == 0
