"""ipda signal strategy: Buy Chance / Sell Chance reversal (or Supertrend) replay.

Reuses the live strategy code verbatim — :class:`ipda.strategy`
``ReversalSignalStrategy`` / ``SupertrendSignalStrategy`` evaluate the same
crossing predicates the service trades. The backtest difference is timing:
live fires mid-formation on the forming bar, while here each closed bar is
evaluated once at its close (close-confirmed; see the README repaint note),
and the fill is the next bar open — the same fill policy as paper.

One entry per bucket is inherent: each closed bar stages at most one intent,
and the engine deduplicates on the intent id.
"""

from __future__ import annotations

from ipda.candles import AggregatedSeries
from ipda.candles import Candle as IpdaCandle
from ipda.strategy import (
    ReversalParams,
    ReversalSignalStrategy,
    StrategyParams,
    SupertrendSignalStrategy,
)

from ..models import Candle, IpdaParams
from . import signals
from .facade import StrategyEngine

NAME = "ipda"

session_driven = False


def _params(engine: StrategyEngine) -> IpdaParams:
    return IpdaParams.model_validate(engine.params.strategy_params)


def _state(engine: StrategyEngine) -> dict[str, object]:
    state = engine.strategy_state.get(NAME)
    if not isinstance(state, dict):
        state = {"bars": [], "skipped": set()}
        engine.strategy_state[NAME] = state
    return state  # type: ignore[return-value]


def on_bar(engine: StrategyEngine, bar: Candle) -> None:
    params = _params(engine)
    state = _state(engine)
    bars: list[Candle] = state["bars"]  # type: ignore[assignment]
    bars.append(bar)
    if len(bars) < 2:
        return

    history = [
        IpdaCandle(
            start=signals.bar_open_ts(engine, b),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            closed=True,
        )
        for b in bars
    ]
    series = AggregatedSeries(closed=history[:-1], forming=history[-1])
    pip_size = engine.params.pip_size

    if params.trigger == "supertrend":
        strategy = SupertrendSignalStrategy(
            StrategyParams(
                sensitivity=params.supertrend_sensitivity,
                atr_len=params.supertrend_atr_len,
                sma_len=params.sma_len,
                risk_reward=params.risk_reward,
                use_hard_targets=True,
                stop_loss_pips=params.stop_loss_pips,
                take_profit_pips=params.take_profit_pips,
                pip_size=pip_size,
            )
        )
    else:
        strategy = ReversalSignalStrategy(
            ReversalParams(
                rsi_len=params.rsi_len,
                oversold=params.oversold,
                overbought=params.overbought,
                stop_loss_pips=params.stop_loss_pips,
                take_profit_pips=params.take_profit_pips,
                pip_size=pip_size,
            )
        )
    decision = strategy.evaluate(series)
    if decision is None:
        return

    open_ts = signals.bar_open_ts(engine, bar)
    label = signals.session_label(engine, open_ts, NAME)
    if label is None:
        skipped: set[str] = state["skipped"]  # type: ignore[assignment]
        if params.enforce_sessions and bar.ts.isoformat() not in skipped:
            skipped.add(bar.ts.isoformat())
            signals.skip_out_of_session(
                engine,
                strategy=NAME,
                session_names=",".join(window.name for window in engine.windows),
                ts=bar.ts,
                detail={"direction": decision.direction, "trigger": decision.trigger},
            )
        return

    entry = decision.entry
    if decision.stop_loss_distance is not None:
        sl_dist = decision.stop_loss_distance
        tp_dist = decision.take_profit_distance
        if tp_dist is None:
            tp_dist = sl_dist * engine.params.rr
    elif decision.stop_loss is not None:
        sl_dist = abs(entry - decision.stop_loss)
        if decision.take_profit is not None:
            tp_dist = abs(decision.take_profit - entry)
        else:
            tp_dist = sl_dist * params.risk_reward
    else:
        return
    if sl_dist <= 0 or tp_dist <= 0:
        return
    is_long = decision.direction == "buy"
    sl_price = entry - sl_dist if is_long else entry + sl_dist
    tp_price = entry + tp_dist if is_long else entry - tp_dist
    signals.stage(
        engine,
        strategy=NAME,
        session=label,
        side="long" if is_long else "short",
        ref_entry=entry,
        sl_price=sl_price,
        tp_price=tp_price,
        signal_ts=bar.ts,
        anchor_to_fill=True,
        detail={"trigger": decision.trigger, "trigger_value": decision.trigger_value},
    )
