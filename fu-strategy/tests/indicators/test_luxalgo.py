"""Unit tests for app/indicators/luxalgo.py — RMA, RSI, and reversal detection."""
import numpy as np
import pandas as pd
import pytest

from app.core.types import Direction
from app.indicators.luxalgo import (
    LuxAlgoReversalEvent,
    calculate_rsi,
    detect_latest_reversal,
    detect_reversals,
    rma,
)


def _make_df(closes: list) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame with the given close prices."""
    n = len(closes)
    times = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * n,
        },
        index=times,
    )


# ── RMA ───────────────────────────────────────────────────────────────────────

class TestRMA:
    def test_flat_series_stays_flat(self):
        """RMA of a constant series should equal that constant."""
        s = pd.Series([10.0] * 50)
        result = rma(s, 14)
        assert abs(result.iloc[-1] - 10.0) < 1e-6

    def test_rma_length_1_equals_input(self):
        """RMA with length=1 (alpha=1) should equal the source exactly."""
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        s = pd.Series(data)
        result = rma(s, 1)
        for i, v in enumerate(data):
            assert abs(result.iloc[i] - v) < 1e-9

    def test_rma_converges_after_warmup(self):
        """After sufficient bars the RMA should settle close to the true value."""
        s = pd.Series([100.0] * 100)
        result = rma(s, 14)
        assert abs(result.iloc[-1] - 100.0) < 1e-4


# ── RSI ───────────────────────────────────────────────────────────────────────

class TestRSI:
    def test_all_gains_returns_100(self):
        """A strictly rising series should give RSI close to 100."""
        closes = list(range(1, 60))
        s = pd.Series([float(c) for c in closes])
        result = calculate_rsi(s, 14)
        # After warmup RSI should be very high
        assert result.iloc[-1] > 90.0

    def test_all_losses_returns_0(self):
        """A strictly falling series should give RSI close to 0."""
        closes = list(range(60, 0, -1))
        s = pd.Series([float(c) for c in closes])
        result = calculate_rsi(s, 14)
        assert result.iloc[-1] < 10.0

    def test_rsi_bounds(self):
        """RSI values must always be in [0, 100]."""
        import random
        random.seed(42)
        closes = [100.0 + random.gauss(0, 1) for _ in range(200)]
        s = pd.Series(closes)
        result = calculate_rsi(s, 14)
        valid = result.dropna()
        assert (valid >= 0.0).all()
        assert (valid <= 100.0).all()

    def test_rsi_sensitivity_10(self):
        """With sensitivity=10 the RSI should still be in valid range."""
        closes = [float(x) for x in range(1, 51)]
        s = pd.Series(closes)
        result = calculate_rsi(s, 10)
        valid = result.dropna()
        assert (valid >= 0.0).all()
        assert (valid <= 100.0).all()


# ── Reversal Detection ─────────────────────────────────────────────────────────

class TestDetectReversals:
    def test_empty_df_returns_no_events(self):
        df = _make_df([100.0])
        assert detect_reversals(df, sensitivity=10) == []

    def test_flat_series_no_reversals(self):
        """A flat series stays at RSI=50 — no crossovers expected."""
        df = _make_df([100.0] * 100)
        events = detect_reversals(df, sensitivity=10)
        assert events == []

    def test_reversal_up_detected(self):
        """After a big drop (RSI → 0) then a big surge, RSI crosses oversold→ Up."""
        # 50 bars falling hard → RSI below 25
        falls = [1000.0 - i * 10 for i in range(50)]
        # 20 bars rising hard → RSI crosses back up through 25
        rises = [falls[-1] + i * 15 for i in range(1, 21)]
        df = _make_df(falls + rises)
        events = detect_reversals(df, sensitivity=10, overbought=75.0, oversold=25.0)
        up_events = [e for e in events if e.direction == Direction.BUY]
        assert len(up_events) >= 1

    def test_reversal_down_detected(self):
        """After a big rally (RSI → 100) then a big drop, RSI crosses overbought→ Down."""
        rises = [1000.0 + i * 10 for i in range(50)]
        falls = [rises[-1] - i * 15 for i in range(1, 21)]
        df = _make_df(rises + falls)
        events = detect_reversals(df, sensitivity=10, overbought=75.0, oversold=25.0)
        down_events = [e for e in events if e.direction == Direction.SELL]
        assert len(down_events) >= 1

    def test_event_has_correct_direction_and_price(self):
        rises = [1000.0 + i * 10 for i in range(50)]
        falls = [rises[-1] - i * 15 for i in range(1, 21)]
        df = _make_df(rises + falls)
        events = detect_reversals(df, sensitivity=10, overbought=75.0, oversold=25.0)
        for e in events:
            assert isinstance(e, LuxAlgoReversalEvent)
            assert e.direction in (Direction.BUY, Direction.SELL)
            assert e.price > 0
            assert 0.0 <= e.rsi_value <= 100.0
            assert e.open == df.loc[e.time, "open"]
            assert e.high == df.loc[e.time, "high"]
            assert e.low == df.loc[e.time, "low"]
            assert e.close == df.loc[e.time, "close"]


# ── detect_latest_reversal ─────────────────────────────────────────────────────

class TestDetectLatestReversal:
    def test_returns_none_for_short_df(self):
        df = _make_df([100.0])
        assert detect_latest_reversal(df, sensitivity=10) is None

    def test_returns_none_when_no_reversal_on_last_bar(self):
        """Flat data: no crossover so latest reversal should be None."""
        df = _make_df([100.0] * 50)
        assert detect_latest_reversal(df, sensitivity=10) is None

    def test_returns_event_only_for_last_bar(self):
        """If a reversal occurs but NOT on the final bar, should return None."""
        # Reversal in the middle, then flat at the end
        falls = [1000.0 - i * 10 for i in range(50)]
        rises = [falls[-1] + i * 15 for i in range(1, 10)]
        flat = [rises[-1]] * 20   # last bars are flat — no crossover here
        df = _make_df(falls + rises + flat)
        result = detect_latest_reversal(df, sensitivity=10)
        assert result is None
