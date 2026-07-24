"""LuxAlgo Supertrend entry, evaluated on the forming target-timeframe bar.

Reproduces file.txt lines 66-83:

    [supertrend, direction] = supertrend(close, sensitivity, 11)
    sma9 = ta.sma(close, 13)
    bull = ta.crossover(close, supertrend)  and close >= sma9   -> BUY
    bear = ta.crossunder(close, supertrend) and close <= sma9   -> SELL

Stop-loss is the supertrend line at signal time (the indicator's own trailing stop);
take-profit is a configurable risk:reward multiple of that stop distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .candles import AggregatedSeries, Candle
from .indicators import crossover, crossunder, sma, supertrend


@dataclass(slots=True)
class StrategyParams:
    sensitivity: float = 5.5
    atr_len: int = 11
    sma_len: int = 13
    risk_reward: float = 2.0
    send_stop_loss: bool = True
    send_take_profit: bool = True


@dataclass(slots=True)
class Decision:
    direction: str  # "buy" | "sell"
    bucket_start: datetime
    entry: float
    stop_loss: float | None
    take_profit: float | None
    supertrend: float


class SupertrendSignalStrategy:
    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    def evaluate(self, series: AggregatedSeries) -> Decision | None:
        candles: list[Candle] = series.as_series()
        if series.forming is None:
            return None
        i = len(candles) - 1  # forming bar is the last element

        closes: list[float | None] = [c.close for c in candles]
        st, _direction = supertrend(candles, self.params.sensitivity, self.params.atr_len)
        sma_vals = sma([c.close for c in candles], self.params.sma_len)

        # Still warming up if any input the crossover needs is undefined.
        if st[i] is None or st[i - 1] is None or sma_vals[i] is None:
            return None

        close_i = candles[i].close
        sma_i = sma_vals[i]
        st_i = st[i]

        is_bull = crossover(closes, st, i) and close_i >= sma_i
        is_bear = crossunder(closes, st, i) and close_i <= sma_i
        if not is_bull and not is_bear:
            return None

        direction = "buy" if is_bull else "sell"
        entry = close_i
        risk = abs(entry - st_i)

        stop_loss: float | None = None
        take_profit: float | None = None
        if self.params.send_stop_loss:
            stop_loss = st_i
        if self.params.send_take_profit:
            if direction == "buy":
                take_profit = entry + self.params.risk_reward * risk
            else:
                take_profit = entry - self.params.risk_reward * risk

        return Decision(
            direction=direction,
            bucket_start=series.forming.start,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            supertrend=st_i,
        )
