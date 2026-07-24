from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from lux_algo.candles import AggregatedSeries, Candle
from lux_algo.indicators import crossover, sma, supertrend
from lux_algo.strategy import StrategyParams, SupertrendSignalStrategy


def _rising(n: int) -> list[Candle]:
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


def _first_bull_index(candles: list[Candle], p: StrategyParams) -> int:
    st, _ = supertrend(candles, p.sensitivity, p.atr_len)
    sm = sma([c.close for c in candles], p.sma_len)
    closes: list[float | None] = [c.close for c in candles]
    for i in range(20, len(candles)):
        if st[i] is None or st[i - 1] is None or sm[i] is None:
            continue
        if crossover(closes, st, i) and candles[i].close >= sm[i]:
            return i
    raise AssertionError("no bull trigger in fixture")


BASE = dict(sensitivity=1.0, atr_len=11, sma_len=13, risk_reward=2.0)


def test_confluence_off_matches_raw_trigger() -> None:
    p = StrategyParams(**BASE, confluence_mode="off")
    candles = _rising(300)
    i = _first_bull_index(candles, p)
    d = SupertrendSignalStrategy(p).evaluate(AggregatedSeries(candles[:i], candles[i]))
    assert d is not None and d.direction == "buy"


def test_unanimous_passes_when_uptrend_overlays_agree() -> None:
    # On a rising series every default overlay is bullish, so a bull trigger survives.
    p = StrategyParams(**BASE, confluence_mode="unanimous")
    candles = _rising(300)
    i = _first_bull_index(candles, p)
    d = SupertrendSignalStrategy(p).evaluate(AggregatedSeries(candles[:i], candles[i]))
    assert d is not None
    assert set(d.confluence) == {"range_filter", "superichi", "tbo", "smart_trail"}
    assert all(v == 1 for v in d.confluence.values())


def test_passes_confluence_logic() -> None:
    strat = SupertrendSignalStrategy(StrategyParams(**BASE))

    strat.params.confluence_mode = "unanimous"
    assert strat._passes_confluence({"a": 1, "b": 1}, want=1) is True
    assert strat._passes_confluence({"a": 1, "b": -1}, want=1) is False
    assert strat._passes_confluence({"a": 1, "b": 0}, want=1) is False  # neutral blocks

    strat.params.confluence_mode = "threshold"
    strat.params.confluence_threshold = 2
    assert strat._passes_confluence({"a": 1, "b": 1, "c": -1}, want=1) is True
    assert strat._passes_confluence({"a": 1, "b": -1, "c": -1}, want=1) is False

    strat.params.confluence_mode = "off"
    assert strat._passes_confluence({"a": -1, "b": -1}, want=1) is True


def test_veto_blocks_entry() -> None:
    # A buy is vetoed when a reversal-down is present against it.
    p = StrategyParams(**BASE, confluence_mode="off", veto_reversals=True)
    strat = SupertrendSignalStrategy(p)

    candles = _rising(300)
    i = _first_bull_index(candles, p)
    # Directly exercise the veto helper with a monkeypatched reversal series.
    import lux_algo.strategy as strategy_mod

    n = len(candles)
    revup = [False] * n
    revdn = [False] * n
    revdn[i] = True
    strategy_mod.reversals = lambda _c: (revup, revdn)  # type: ignore[assignment]
    try:
        d = strat.evaluate(AggregatedSeries(candles[:i], candles[i]))
    finally:
        # restore
        from lux_algo.overlays import reversals as real_reversals

        strategy_mod.reversals = real_reversals  # type: ignore[assignment]
    assert d is None
