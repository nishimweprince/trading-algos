"""The same bar must always produce the same context features.

Before the labeling window became a fixed bar count, `compute_context` saw
whatever range the operator's replay session happened to span, so EMA200/RSI/ATR
were computed over a different amount of history every time — and the ATR
terciles were derived from the full fetched frame, forward bars included.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.context import compute_context

CONTEXT_KEYS = ("trend_state", "atr_bucket", "rsi_band", "atr_at_signal")


def _synthetic(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.4, n))
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.ones(n),
        }
    )


def test_context_ignores_bars_after_the_signal():
    """Adding forward bars must not move a single context value.

    This is the leak: the terciles used to be computed over the whole frame, so
    the volatility bucket at the signal depended on what happened afterwards.
    """
    candles = _synthetic(700)
    signal_idx = 600

    short_frame = compute_context(candles.iloc[: signal_idx + 1].reset_index(drop=True), signal_idx)
    long_frame = compute_context(candles, signal_idx)

    for key in CONTEXT_KEYS:
        assert short_frame[key] == long_frame[key], key


def test_context_is_stable_for_a_fixed_warmup():
    """Two sessions covering different ranges, same warmup count, same answer."""
    candles = _synthetic(1200)
    signal_idx = 900
    warmup = settings.warmup_bars

    window_a = candles.iloc[signal_idx - warmup + 1 : signal_idx + 1].reset_index(drop=True)
    window_b = candles.iloc[signal_idx - warmup + 1 : signal_idx + 40].reset_index(drop=True)

    ctx_a = compute_context(window_a, warmup - 1)
    ctx_b = compute_context(window_b, warmup - 1)

    for key in CONTEXT_KEYS:
        assert ctx_a[key] == ctx_b[key], key


def test_short_history_is_marked_unreliable():
    """A signal near the start of a symbol's history cannot have a real EMA200."""
    thin = compute_context(_synthetic(50), 49)
    assert thin["context_reliable"] is False
    assert thin["warmup_bars_available"] == 50

    deep = compute_context(_synthetic(settings.ema_period + 10), settings.ema_period + 9)
    assert deep["context_reliable"] is True


def test_raw_values_accompany_the_buckets():
    """Buckets throw information away; the raw values let thresholds be re-cut
    later without re-labelling anything."""
    candles = _synthetic(400)
    ctx = compute_context(candles, 399)
    signal_close = float(candles["close"].iloc[399])

    assert 0 <= ctx["rsi_value"] <= 100
    assert ctx["atr_pct"] > 0
    assert ctx["dist_ema_atr"] == pytest.approx(
        (signal_close - ctx["ema_value"]) / ctx["atr_at_signal"]
    )
    assert len(ctx["atr_terciles"]) == 2
    assert ctx["atr_terciles"][0] <= ctx["atr_terciles"][1]
