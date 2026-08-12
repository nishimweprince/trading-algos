"""The live entry trigger: the Buy Chance / Sell Chance labels of file.txt Section 11."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ipda.candles import AggregatedSeries, Candle
from ipda.indicators import rsi
from ipda.strategy import ReversalParams, ReversalSignalStrategy

PARAMS = ReversalParams(
    rsi_len=14,
    oversold=25.0,
    overbought=75.0,
    stop_loss_pips=40.0,
    take_profit_pips=50.0,
    pip_size=0.0001,
)


def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 14, 8, 0, tzinfo=UTC)
    out: list[Candle] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        out.append(
            Candle(
                start=start + timedelta(minutes=5 * i),
                open=prev,
                high=max(prev, close),
                low=min(prev, close),
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return out


def _series(closes: list[float]) -> AggregatedSeries:
    candles = _candles(closes)
    return AggregatedSeries(closed=candles[:-1], forming=candles[-1])


def _decline_then_rally() -> list[float]:
    """Long slide to drive RSI under 25, then a rally that crosses back up."""
    closes = [100.0]
    for _ in range(40):
        closes.append(closes[-1] - 1.0)
    for _ in range(4):
        closes.append(closes[-1] + 6.0)
    return closes


def _rally_then_decline() -> list[float]:
    closes = [100.0]
    for _ in range(40):
        closes.append(closes[-1] + 1.0)
    for _ in range(4):
        closes.append(closes[-1] - 6.0)
    return closes


def _first_cross_up(closes: list[float]) -> int:
    values = rsi(closes, PARAMS.rsi_len)
    for i in range(1, len(values)):
        if values[i] is None or values[i - 1] is None:
            continue
        if values[i] > 25.0 and values[i - 1] <= 25.0:
            return i
    raise AssertionError("fixture never crosses up through 25")


def _first_cross_down(closes: list[float]) -> int:
    values = rsi(closes, PARAMS.rsi_len)
    for i in range(1, len(values)):
        if values[i] is None or values[i - 1] is None:
            continue
        if values[i] < 75.0 and values[i - 1] >= 75.0:
            return i
    raise AssertionError("fixture never crosses down through 75")


def test_buy_chance_on_cross_up_through_oversold() -> None:
    closes = _decline_then_rally()
    i = _first_cross_up(closes)

    decision = ReversalSignalStrategy(PARAMS).evaluate(_series(closes[: i + 1]))

    assert decision is not None
    assert decision.direction == "buy"
    assert decision.trigger == "reversal"
    assert decision.trigger_value > 25.0
    assert decision.entry == closes[i]


def test_sell_chance_on_cross_down_through_overbought() -> None:
    closes = _rally_then_decline()
    i = _first_cross_down(closes)

    decision = ReversalSignalStrategy(PARAMS).evaluate(_series(closes[: i + 1]))

    assert decision is not None
    assert decision.direction == "sell"
    assert decision.trigger_value < 75.0


def test_targets_are_fixed_pip_distances() -> None:
    closes = _decline_then_rally()
    i = _first_cross_up(closes)

    decision = ReversalSignalStrategy(PARAMS).evaluate(_series(closes[: i + 1]))

    assert decision is not None
    # Distances, not levels: mt5-trader anchors them to the fill, not the bar close.
    assert decision.stop_loss is None
    assert decision.take_profit is None
    assert abs(decision.stop_loss_distance - 40.0 * 0.0001) < 1e-12
    assert abs(decision.take_profit_distance - 50.0 * 0.0001) < 1e-12


def test_deep_in_oversold_without_a_cross_is_not_a_signal() -> None:
    """RSI merely being below 25 is not the trigger — it has to cross back up."""
    closes = [100.0 - i for i in range(40)]

    assert ReversalSignalStrategy(PARAMS).evaluate(_series(closes)) is None


def test_no_signal_during_warmup() -> None:
    assert ReversalSignalStrategy(PARAMS).evaluate(_series([100.0, 101.0, 102.0])) is None


def test_no_forming_bar_returns_none() -> None:
    series = AggregatedSeries(closed=_candles(_decline_then_rally()), forming=None)

    assert ReversalSignalStrategy(PARAMS).evaluate(series) is None


def test_send_flags_suppress_each_leg() -> None:
    closes = _decline_then_rally()
    i = _first_cross_up(closes)
    params = ReversalParams(
        rsi_len=14, send_stop_loss=False, send_take_profit=False, pip_size=0.0001
    )

    decision = ReversalSignalStrategy(params).evaluate(_series(closes[: i + 1]))

    assert decision is not None
    assert decision.stop_loss_distance is None
    assert decision.take_profit_distance is None
