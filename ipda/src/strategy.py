"""Entry triggers ported from file.txt, evaluated on the forming bar.

Two independent signal families live in the Pine script. The service trades the
**reversal** one; the supertrend one is kept because it is a faithful port and the
alternative configuration, but it is not what fires an order.

Reversal — "Buy Chance" / "Sell Chance" labels (file.txt Section 11)::

    rev_src = rsi(close, ReversalInputs)              # ReversalInputs = 14
    revup = ta.crossover (rev_src, oversold)          # 25 -> BUY  ("Buy Chance")
    revdn = ta.crossunder(rev_src, overbought)        # 75 -> SELL ("Sell Chance")

Note that the Pine gates both behind ``enableReversal``, which ships **false** — the
labels only appear on a chart once "Reversal Signal" is ticked under IPDA Settings.
Here the reversal trigger is the configured entry, so it is always live.

Supertrend — the ▲/▼ labels (file.txt Section 5)::

    [supertrend, _] = supertrend(close, sensitivity, 11)
    bull = ta.crossover(close, supertrend)  and close >= sma(close, 13)   -> BUY
    bear = ta.crossunder(close, supertrend) and close <= sma(close, 13)   -> SELL

No confluence overlays or vetoes in either case.

Targets: with ``use_hard_targets`` (the default, and mandatory for the reversal
trigger) SL/TP are emitted as fixed pip distances. The supertrend trigger can instead
anchor the stop to its own line and take a risk:reward multiple for the target — an
option the reversal trigger does not have, because RSI produces no price level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .candles import AggregatedSeries, Candle
from .indicators import constant_series, crossover, crossunder, rsi, sma, supertrend

REVERSAL = "reversal"
SUPERTREND = "supertrend"


@dataclass(slots=True)
class StrategyParams:
    sensitivity: float = 5.5
    atr_len: int = 11
    sma_len: int = 13
    risk_reward: float = 2.0
    send_stop_loss: bool = True
    send_take_profit: bool = True
    use_hard_targets: bool = False
    stop_loss_pips: float = 40.0
    take_profit_pips: float = 50.0
    pip_size: float = 0.0001


@dataclass(slots=True)
class ReversalParams:
    rsi_len: int = 14
    oversold: float = 25.0
    overbought: float = 75.0
    send_stop_loss: bool = True
    send_take_profit: bool = True
    stop_loss_pips: float = 40.0
    take_profit_pips: float = 50.0
    pip_size: float = 0.0001


@dataclass(slots=True)
class Decision:
    direction: str  # "buy" | "sell"
    bucket_start: datetime
    entry: float
    stop_loss: float | None
    take_profit: float | None
    trigger: str  # "reversal" | "supertrend"
    trigger_value: float  # RSI reading, or the supertrend line
    stop_loss_distance: float | None = None
    take_profit_distance: float | None = None


def _hard_targets(
    *,
    send_stop_loss: bool,
    send_take_profit: bool,
    stop_loss_pips: float,
    take_profit_pips: float,
    pip_size: float,
) -> tuple[float | None, float | None]:
    stop = stop_loss_pips * pip_size if send_stop_loss else None
    target = take_profit_pips * pip_size if send_take_profit else None
    return stop, target


class ReversalSignalStrategy:
    """RSI crossing out of oversold/overbought — the Buy Chance / Sell Chance labels."""

    def __init__(self, params: ReversalParams) -> None:
        self.params = params

    def evaluate(self, series: AggregatedSeries) -> Decision | None:
        if series.forming is None:
            return None
        candles: list[Candle] = series.as_series()
        i = len(candles) - 1

        rsi_vals = rsi([c.close for c in candles], self.params.rsi_len)
        if rsi_vals[i] is None or rsi_vals[i - 1] is None:
            return None

        n = len(candles)
        oversold = constant_series(self.params.oversold, n)
        overbought = constant_series(self.params.overbought, n)

        is_buy = crossover(rsi_vals, oversold, i)
        is_sell = crossunder(rsi_vals, overbought, i)
        if not is_buy and not is_sell:
            return None

        stop_distance, target_distance = _hard_targets(
            send_stop_loss=self.params.send_stop_loss,
            send_take_profit=self.params.send_take_profit,
            stop_loss_pips=self.params.stop_loss_pips,
            take_profit_pips=self.params.take_profit_pips,
            pip_size=self.params.pip_size,
        )
        return Decision(
            direction="buy" if is_buy else "sell",
            bucket_start=series.forming.start,
            entry=candles[i].close,
            stop_loss=None,
            take_profit=None,
            trigger=REVERSAL,
            trigger_value=rsi_vals[i],
            stop_loss_distance=stop_distance,
            take_profit_distance=target_distance,
        )


class SupertrendSignalStrategy:
    """The ▲/▼ Supertrend×SMA entry. Kept as the alternative trigger."""

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
            stop_loss_distance, take_profit_distance = _hard_targets(
                send_stop_loss=self.params.send_stop_loss,
                send_take_profit=self.params.send_take_profit,
                stop_loss_pips=self.params.stop_loss_pips,
                take_profit_pips=self.params.take_profit_pips,
                pip_size=self.params.pip_size,
            )
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
            trigger=SUPERTREND,
            trigger_value=st_i,
            stop_loss_distance=stop_loss_distance,
            take_profit_distance=take_profit_distance,
        )
