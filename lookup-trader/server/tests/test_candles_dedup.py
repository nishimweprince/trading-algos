"""A bar written under both parquet layouts must reach every reader once.

The chart asserts its input is strictly ascending and blanks the whole page on a
repeat, so this is a crash, not a rounding error. The warmup window is the
quieter half of the same bug: a duplicated bar shifts every preceding bar one
place further back, which silently changes the context features of the bar being
labelled.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
import app.db.duck as duck_mod
from app.db.duck import get_connection, register_candles_view
from app.main import app
from app.services.candles import fetch_candles, fetch_labeling_window

SYMBOL, TIMEFRAME = "DUPFX", "H1"
START = pd.Timestamp("2024-01-02 00:00:00", tz="UTC")


def _frame(times: pd.DatetimeIndex, high: float = 1.2, low: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": times,
            "open": 1.1,
            "high": high,
            "low": low,
            "close": 1.15,
            "volume": 100.0,
        }
    )


@pytest.fixture
def duplicated_store(tmp_path, monkeypatch):
    """Ten bars in the month layout, the first three repeated in the legacy one.

    Yields the shared connection with the view pointed at this store, and puts
    the real one back afterwards — the view lives in the database catalog, so a
    test that left it pointing at a temp directory would break every later test.
    """
    times = pd.date_range(START, periods=10, freq="h")
    root = tmp_path / "candles" / f"symbol={SYMBOL}" / f"timeframe={TIMEFRAME}" / "year=2024"
    month_dir = root / "month=01"
    month_dir.mkdir(parents=True)
    _frame(times, high=1.9, low=0.5).to_parquet(month_dir / "part-000.parquet", index=False)
    # The legacy copy carries the narrower range, as a truncated boundary bar does.
    _frame(times[:3]).to_parquet(root / "part-000.parquet", index=False)

    con = get_connection()
    monkeypatch.setattr(config_mod.settings, "data_dir", tmp_path)
    duck_mod._view_ready.clear()
    register_candles_view(con, force=True)
    try:
        yield con
    finally:
        monkeypatch.undo()
        duck_mod._view_ready.clear()
        register_candles_view(con, force=True)


def test_the_endpoint_returns_one_bar_per_timestamp(duplicated_store):
    client = TestClient(app)
    r = client.get(
        "/candles",
        params={
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-01-31T00:00:00Z",
        },
    )
    assert r.status_code == 200
    bars = r.json()

    stamps = [b["ts"] for b in bars]
    assert len(stamps) == len(set(stamps)) == 10


def test_the_endpoint_returns_bars_strictly_ascending(duplicated_store):
    """What the charting library actually asserts on."""
    client = TestClient(app)
    bars = client.get(
        "/candles",
        params={
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-01-31T00:00:00Z",
        },
    ).json()

    stamps = [b["ts"] for b in bars]
    assert all(a < b for a, b in zip(stamps, stamps[1:]))


def test_the_bar_count_is_not_inflated(duplicated_store):
    client = TestClient(app)
    bounds = client.get(
        "/candles/bounds", params={"symbol": SYMBOL, "timeframe": TIMEFRAME}
    ).json()

    assert bounds["bar_count"] == 10


def test_fetch_candles_keeps_the_month_copy(duplicated_store):
    frame = fetch_candles(
        duplicated_store,
        SYMBOL,
        TIMEFRAME,
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 31, tzinfo=timezone.utc),
    )

    assert len(frame) == 10
    # The legacy rows would have brought 1.2 / 1.0.
    assert frame["high"].unique().tolist() == pytest.approx([1.9])
    assert frame["low"].unique().tolist() == pytest.approx([0.5])


def test_the_warmup_window_is_not_shifted_by_a_duplicate(duplicated_store):
    """`signal_idx` counts back over the history, so an extra row moves the
    anchor onto the wrong bar — and `fetch_labeling_window` raises when the bar
    it lands on is not the one that was asked for."""
    signal_ts = (START + pd.Timedelta(hours=5)).to_pydatetime()

    window, signal_idx = fetch_labeling_window(
        duplicated_store,
        SYMBOL,
        TIMEFRAME,
        signal_ts,
        warmup_bars=600,
        forward_bars=0,
    )

    assert len(window) == 6
    assert signal_idx == 5
    assert window["ts"].is_unique
    assert window["ts"].is_monotonic_increasing
