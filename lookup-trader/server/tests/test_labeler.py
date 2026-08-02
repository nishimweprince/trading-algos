import pytest
import pandas as pd

from app.services.labeler import label_triple_barrier


def _make_candles(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_long_win():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 102, 99, 101),   # signal at idx 0
        (101, 105, 100, 104),  # hits tp=104
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "win"
    assert result["realized_r"] == pytest.approx(2.0)


def test_long_loss():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 97, 98),   # hits sl=98
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "loss"
    assert result["realized_r"] == pytest.approx(-1.0)


def test_short_win():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 94, 95),   # hits tp=95 for short
    ])
    result = label_triple_barrier(candles, 0, side=-1, entry=100, sl=102, tp=95, max_bars=5)
    assert result["result"] == "win"


def test_timeout():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=90, tp=110, max_bars=2)
    assert result["result"] == "timeout"
    assert result["realized_r"] is None


def test_ambiguous_conservative():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),  # both tp and sl hit
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="conservative"
    )
    assert result["result"] == "loss"


def test_ambiguous_drop():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="drop"
    )
    assert result["result"] == "ambiguous"


def test_ambiguous_optimistic():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="optimistic"
    )
    assert result["result"] == "win"


def test_signal_at_last_bar():
    candles = _make_candles([(100, 101, 99, 100)])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "timeout"
