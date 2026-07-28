"""LuxAlgo Supertrend entry with overlay confluence, evaluated on the forming bar.

Trigger (file.txt:66-83) — decided purely on the Supertrend, exactly as the Pine script
decides the buy/sell labels:

    [supertrend, direction] = supertrend(close, sensitivity, 11)
    sma9 = ta.sma(close, 13)
    bull = ta.crossover(close, supertrend)  and close >= sma9   -> BUY
    bear = ta.crossunder(close, supertrend) and close <= sma9   -> SELL

Confluence — the trigger is then gated by the directional overlays from file.txt (Range
Filter, SuperIchi, TBO, Smart Trail, HA bias, MACD color, PSAR). Each is individually
toggleable; ``mode`` controls how strict the agreement must be:
  - "unanimous": every enabled overlay must confirm the trigger direction
  - "threshold": at least ``threshold`` enabled overlays must confirm
  - "off":       no gating -> reproduces the raw Pine ▲/▼

Vetoes — optionally, an opposing counter-trend marker blocks the entry: a major TP Point
(lele) or an RSI Reversal against the trade direction.

Stop-loss is the supertrend line at signal time; take-profit is a risk:reward multiple.
When ``use_hard_targets`` is set, SL/TP are fixed dollar P&L amounts from entry,
converted to price distance using volume and contract size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .candles import AggregatedSeries, Candle
from .indicators import crossover, crossunder, sma, supertrend
from .overlays import (
    ha_bias_dir,
    macd_dir,
    psar_dir,
    range_filter_dir,
    reversals,
    smart_trail_dir,
    superichi_dir,
    tbo_dir,
    tp_points,
)

# name -> direction-series function, in a stable order
OVERLAYS = {
    "range_filter": range_filter_dir,
    "superichi": superichi_dir,
    "tbo": tbo_dir,
    "smart_trail": smart_trail_dir,
    "ha_bias": ha_bias_dir,
    "macd_color": macd_dir,
    "psar": psar_dir,
}


@dataclass(slots=True)
class StrategyParams:
    sensitivity: float = 5.5
    atr_len: int = 11
    sma_len: int = 13
    risk_reward: float = 2.0
    send_stop_loss: bool = True
    send_take_profit: bool = True
    use_hard_targets: bool = False
    hard_stop_loss_usd: float = 25.0
    hard_take_profit_usd: float = 40.0
    volume: float = 0.01
    contract_size: float = 100.0
    # Confluence
    confluence_mode: str = "unanimous"  # unanimous | threshold | off
    confluence_threshold: int = 0  # used when mode == "threshold"; 0 -> "all enabled"
    enabled_overlays: frozenset[str] = field(
        default_factory=lambda: frozenset({"range_filter", "superichi", "tbo", "smart_trail"})
    )
    # Vetoes
    veto_tp_points: bool = False
    veto_reversals: bool = False


@dataclass(slots=True)
class Decision:
    direction: str  # "buy" | "sell"
    bucket_start: datetime
    entry: float
    stop_loss: float | None
    take_profit: float | None
    supertrend: float
    confluence: dict[str, int] = field(default_factory=dict)


class SupertrendSignalStrategy:
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

        confluence = self._overlay_dirs(candles, i)
        if not self._passes_confluence(confluence, want):
            return None
        if self._vetoed(candles, i, want):
            return None

        entry = close_i
        stop_loss: float | None = None
        take_profit: float | None = None
        if self.params.use_hard_targets:
            notional = self.params.volume * self.params.contract_size
            sl_dist = self.params.hard_stop_loss_usd / notional
            tp_dist = self.params.hard_take_profit_usd / notional
            if self.params.send_stop_loss:
                stop_loss = entry - want * sl_dist
            if self.params.send_take_profit:
                take_profit = entry + want * tp_dist
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
            confluence=confluence,
        )

    def _overlay_dirs(self, candles: list[Candle], i: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for name in self.params.enabled_overlays:
            fn = OVERLAYS.get(name)
            if fn is None:
                continue
            series = fn(candles)  # type: ignore[operator]
            value = series[i] if series[i] is not None else 0
            result[name] = int(value)
        return result

    def _passes_confluence(self, confluence: dict[str, int], want: int) -> bool:
        mode = self.params.confluence_mode
        if mode == "off" or not confluence:
            return True
        confirmations = sum(1 for v in confluence.values() if v == want)
        if mode == "threshold":
            needed = self.params.confluence_threshold or len(confluence)
            return confirmations >= needed
        # unanimous
        return confirmations == len(confluence)

    def _vetoed(self, candles: list[Candle], i: int, want: int) -> bool:
        if self.params.veto_tp_points:
            tp = tp_points(candles)[i]
            # major SELL TP (-1) vetoes a buy; major BUY TP (1) vetoes a sell
            if (want == 1 and tp == -1) or (want == -1 and tp == 1):
                return True
        if self.params.veto_reversals:
            revup, revdn = reversals(candles)
            if (want == 1 and revdn[i]) or (want == -1 and revup[i]):
                return True
        return False
