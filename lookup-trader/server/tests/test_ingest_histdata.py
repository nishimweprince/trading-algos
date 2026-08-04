from pathlib import Path
import shutil

import pandas as pd
import pytest

# Import from scripts path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from ingest_histdata import (  # noqa: E402
    _detect_format,
    _month_partition_path,
    _parse_histdata_minute_file,
    _parse_histdata_tick_file,
    _ticks_to_ohlc,
    _write_month_partition,
    ingest,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_detect_tick_format():
    path = FIXTURES / "sample_histdata_tick.csv"
    assert _detect_format(path) == "tick"


def test_parse_tick_file():
    df = _parse_histdata_tick_file(FIXTURES / "sample_histdata_tick.csv")
    assert len(df) == 5
    assert "price" in df.columns
    assert df["price"].iloc[0] == pytest.approx((4516.105 + 4516.845) / 2)


def test_ticks_to_h1():
    df = _parse_histdata_tick_file(FIXTURES / "sample_histdata_tick.csv")
    bars = _ticks_to_ohlc(df, "H1")
    assert len(bars) >= 1
    assert set(bars.columns) == {"ts", "open", "high", "low", "close", "volume"}


def test_parse_minute_file():
    df = _parse_histdata_minute_file(FIXTURES / "sample_histdata.csv")
    assert len(df) == 5
    assert df["open"].iloc[0] == pytest.approx(1.1)


def test_detect_semicolon_minute_format():
    path = FIXTURES / "sample_histdata_semicolon.csv"
    assert _detect_format(path) == "minute"


def test_parse_semicolon_minute_file():
    df = _parse_histdata_minute_file(FIXTURES / "sample_histdata_semicolon.csv")
    assert len(df) == 3
    assert df["open"].iloc[0] == pytest.approx(4626.435)
    assert df["ts"].iloc[0] == pd.Timestamp("2026-05-01 00:00:00", tz="UTC")


def test_ingest_writes_separate_month_partitions(tmp_path):
    jan_dir = tmp_path / "jan"
    feb_dir = tmp_path / "feb"
    jan_dir.mkdir()
    feb_dir.mkdir()
    shutil.copy(FIXTURES / "sample_histdata_jan2024.csv", jan_dir / "jan.csv")
    shutil.copy(FIXTURES / "sample_histdata_feb2024.csv", feb_dir / "feb.csv")

    ingest(input_dir=jan_dir, symbol="EURUSD", timeframe="H1", output_dir=tmp_path / "out")
    ingest(input_dir=feb_dir, symbol="EURUSD", timeframe="H1", output_dir=tmp_path / "out")

    out = tmp_path / "out"
    jan = _month_partition_path(out, "EURUSD", "H1", 2024, 1)
    feb = _month_partition_path(out, "EURUSD", "H1", 2024, 2)
    assert jan.exists()
    assert feb.exists()


def test_second_month_ingest_does_not_remove_first(tmp_path):
    jan_dir = tmp_path / "jan"
    jan_dir.mkdir()
    shutil.copy(FIXTURES / "sample_histdata_jan2024.csv", jan_dir / "jan.csv")
    out = tmp_path / "out"
    ingest(input_dir=jan_dir, symbol="EURUSD", timeframe="H1", output_dir=out)

    feb_dir = tmp_path / "feb"
    feb_dir.mkdir()
    shutil.copy(FIXTURES / "sample_histdata_feb2024.csv", feb_dir / "feb.csv")
    ingest(input_dir=feb_dir, symbol="EURUSD", timeframe="H1", output_dir=out)
    jan_path = _month_partition_path(out, "EURUSD", "H1", 2024, 1)
    feb_path = _month_partition_path(out, "EURUSD", "H1", 2024, 2)
    assert jan_path.exists()
    assert feb_path.exists()
    assert len(pd.read_parquet(jan_path)) >= 1
    assert len(pd.read_parquet(feb_path)) >= 1


def test_reingest_same_month_merges_and_dedupes(tmp_path):
    group = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2024-01-02 00:00:00+00:00", "2024-01-02 01:00:00+00:00"], utc=True),
            "open": [1.1, 1.2],
            "high": [1.2, 1.3],
            "low": [1.0, 1.1],
            "close": [1.15, 1.25],
            "volume": [100.0, 110.0],
        }
    )
    out_path = _month_partition_path(tmp_path, "EURUSD", "H1", 2024, 1)
    assert _write_month_partition(out_path, group) == 2

    duplicate = group.iloc[[0]].copy()
    duplicate["close"] = 9.99
    assert _write_month_partition(out_path, duplicate) == 2
    merged = pd.read_parquet(out_path)
    assert len(merged) == 2
    assert merged.loc[merged["ts"] == group["ts"].iloc[0], "close"].iloc[0] == pytest.approx(9.99)
