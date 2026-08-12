"""IPDA Supertrend entry evaluated on the forming bar.

Trigger (file.txt Section 5) — decided purely on the Supertrend, exactly as the Pine
script decides the buy/sell labels:

    [supertrend, direction] = supertrend(close, sensitivity, 11)
    sma9 = ta.sma(close, 13)
    bull = ta.crossover(close, supertrend)  and close >= sma9   -> BUY
    bear = ta.crossunder(close, supertrend) and close <= sma9   -> SELL

No confluence overlays or vetoes — IPDA-only.

Stop-loss is the supertrend line at signal time; take-profit is a risk:reward multiple.
When ``use_hard_targets`` is set, SL/TP are emitted as distances instead.
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
    use_hard_targets: bool = False
    stop_loss_pips: float = 25.0
    take_profit_pips: float = 40.0
    pip_size: float = 0.0001


@dataclass(slots=True)
class Decision:
    direction: str  # "buy" | "sell"
    bucket_start: datetime
    entry: float
    stop_loss: float | None
    take_profit: float | None
    supertrend: float
    stop_loss_distance: float | None = None
    take_profit_distance: float | None = None


class IpdaSignalStrategy:
    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    def evaluate(self, series: AggregatedSeries) -> Decision | None:
        if series.forming is None:
            return None
        candles: list[Candle] = series.as_series()
        i = len(candles) - 1

        closes: list[float | None] = [c.close for c in candles]
        st, _direction = supertrend(candles, self.params.sensitivity, self.params.atr_len)
        sma_vals = sma([c.close for c in candles], self.params.sma_len)

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
        want = 1 if is_bull else -1

        entry = close_i
        stop_loss: float | None = None
        take_profit: float | None = None
        stop_loss_distance: float | None = None
        take_profit_distance: float | None = None
        if self.params.use_hard_targets:
            if self.params.send_stop_loss:
                stop_loss_distance = self.params.stop_loss_pips * self.params.pip_size
            if self.params.send_take_profit:
                take_profit_distance = self.params.take_profit_pips * self.params.pip_size
        else:
            risk = abs(entry - st_i)
            if self.params.send_stop_loss:
                stop_loss = st_i
            if self.params.send_take_profit:
                take_profit = entry + want * self.params.risk_reward * risk

        return Decision(
            direction=direction,
            bucket_start=series.forming.start,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            supertrend=st_i,
            stop_loss_distance=stop_loss_distance,
            take_profit_distance=take_profit_distance,
        )
