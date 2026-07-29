from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from lux_algo.candles import AggregatedSeries, Candle
from lux_algo.indicators import crossover, crossunder, sma, supertrend
from lux_algo.strategy import StrategyParams, SupertrendSignalStrategy

# confluence_mode="off" isolates the Supertrend trigger (confluence is tested separately).
PARAMS = StrategyParams(
    sensitivity=1.0, atr_len=11, sma_len=13, risk_reward=2.0, confluence_mode="off"
)


def _zigzag_candles(n: int) -> list[Candle]:
    """Rising trend with oscillations. The uptrend keeps ``close`` above its SMA while
    the oscillations make the Supertrend flip repeatedly, so an up-flip coincides with
    the ``close >= sma`` filter and a bull signal fires (a symmetric wave flips at the
    extremes, where the filter correctly rejects)."""
    candles: list[Candle] = []
    prev = 100.0
    for i in range(n):
        close = 100.0 + 0.5 * i + 3.0 * math.sin(i * 0.5)
        candles.append(
            Candle(
                start=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=prev,
                high=max(prev, close) + 0.3,
                low=min(prev, close) - 0.3,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return candles


def _first_fire_index(candles: list[Candle]) -> tuple[int, str]:
    st, _ = supertrend(candles, PARAMS.sensitivity, PARAMS.atr_len)
    sma_vals = sma([c.close for c in candles], PARAMS.sma_len)
    closes: list[float | None] = [c.close for c in candles]
    for i in range(20, len(candles)):
        if st[i] is None or st[i - 1] is None or sma_vals[i] is None:
            continue
        bull = crossover(closes, st, i) and candles[i].close >= sma_vals[i]
        bear = crossunder(closes, st, i) and candles[i].close <= sma_vals[i]
        if bull:
            return i, "buy"
        if bear:
            return i, "sell"
    raise AssertionError("no crossover found in fixture")


def test_evaluate_fires_on_forming_bar_with_supertrend_stop() -> None:
    candles = _zigzag_candles(300)
    i, expected_dir = _first_fire_index(candles)

    series = AggregatedSeries(closed=candles[:i], forming=candles[i])
    decision = SupertrendSignalStrategy(PARAMS).evaluate(series)

    assert decision is not None
    assert decision.direction == expected_dir
    assert decision.bucket_start == candles[i].start
    assert decision.entry == candles[i].close
    # SL is the supertrend line; TP is risk_reward * stop distance on the entry side.
    assert decision.stop_loss == decision.supertrend
    risk = abs(decision.entry - decision.supertrend)
    if expected_dir == "buy":
        assert decision.stop_loss < decision.entry
        assert abs(decision.take_profit - (decision.entry + 2.0 * risk)) < 1e-9
    else:
        assert decision.stop_loss > decision.entry
        assert abs(decision.take_profit - (decision.entry - 2.0 * risk)) < 1e-9


def test_no_signal_returns_none_during_warmup() -> None:
    candles = _zigzag_candles(5)
    series = AggregatedSeries(closed=candles[:-1], forming=candles[-1])
    assert SupertrendSignalStrategy(PARAMS).evaluate(series) is None


def test_no_forming_bar_returns_none() -> None:
    series = AggregatedSeries(closed=_zigzag_candles(50), forming=None)
    assert SupertrendSignalStrategy(PARAMS).evaluate(series) is None


def test_hard_targets_use_fixed_pip_sl_tp() -> None:
    candles = _zigzag_candles(300)
    i, expected_dir = _first_fire_index(candles)
    pip_size = 0.0001
    params = StrategyParams(
        sensitivity=1.0,
        atr_len=11,
        sma_len=13,
        confluence_mode="off",
        use_hard_targets=True,
        stop_loss_pips=25.0,
        take_profit_pips=40.0,
        pip_size=pip_size,
    )

    series = AggregatedSeries(closed=candles[:i], forming=candles[i])
    decision = SupertrendSignalStrategy(params).evaluate(series)

    assert decision is not None
    assert decision.direction == expected_dir
    sl_dist = 25.0 * pip_size
    tp_dist = 40.0 * pip_size
    if expected_dir == "buy":
        assert abs(decision.stop_loss - (decision.entry - sl_dist)) < 1e-9
        assert abs(decision.take_profit - (decision.entry + tp_dist)) < 1e-9
    else:
        assert abs(decision.stop_loss - (decision.entry + sl_dist)) < 1e-9
        assert abs(decision.take_profit - (decision.entry - tp_dist)) < 1e-9
