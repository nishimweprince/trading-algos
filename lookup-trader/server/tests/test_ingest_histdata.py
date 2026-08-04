from pathlib import Path

import pandas as pd
import pytest

# Import from scripts path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from ingest_histdata import (  # noqa: E402
    _detect_format,
    _parse_histdata_minute_file,
    _parse_histdata_tick_file,
    _ticks_to_ohlc,
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
