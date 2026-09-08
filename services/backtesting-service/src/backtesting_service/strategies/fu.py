"""fu signal strategy: FU candle replay in ``fu_only`` mode (the shipped default).

The predicate mirrors ``fu-strategy/app/indicators/fu_candle.py`` exactly —
bullish FU is ``low < prev_low and close > prev_high``, bearish is the
mirror — plus the optional doji-leading and SMA filters, evaluated here on
each closed bar (close-confirmed; live fires intrabar on the forming candle).
The stop/target construction mirrors ``confluence.build_signal`` in
``fu_only`` mode: an ATR-fraction buffer beyond the swept extreme, and a risk
multiple for the target. Levels are absolute and indicator-derived, so the
fill keeps them (``anchor_to_fill=False``).

Full HTF confluence (bias + zones gating, ``fu_only=false``) needs the
multi-timeframe bias/zone state the single-timeframe engine does not carry;
``FuParams`` rejects it until that lands (phase 2).
"""

from __future__ import annotations

from ..models import Candle, FuParams
from . import signals
from .facade import StrategyEngine

NAME = "fu"

session_driven = False


def _params(engine: StrategyEngine) -> FuParams:
    return FuParams.model_validate(engine.params.strategy_params)


def _state(engine: StrategyEngine) -> dict[str, object]:
    state = engine.strategy_state.get(NAME)
    if not isinstance(state, dict):
        state = {"bars": []}
        engine.strategy_state[NAME] = state
    return state  # type: ignore[return-value]


def _is_doji(o: float, h: float, low: float, c: float, max_body_ratio: float) -> bool:
    return abs(c - o) <= (h - low) * max_body_ratio


def _atr(bars: list[Candle], length: int) -> float:
    """SMA of true range over the trailing ``length`` bars (fu ``common.atr``)."""
    window = bars[-length:] if len(bars) >= length else bars
    prev_close: float | None = None
    total = 0.0
    for b in window:
        if prev_close is None:
            total += b.high - b.low
        else:
            total += max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        prev_close = b.close
    return total / len(window)


def _sma(closes: list[float], length: int) -> float | None:
    if len(closes) < length:
        return None
    return sum(closes[-length:]) / length


def on_bar(engine: StrategyEngine, bar: Candle) -> None:
    params = _params(engine)
    state = _state(engine)
    bars: list[Candle] = state["bars"]  # type: ignore[assignment]
    bars.append(bar)
    if len(bars) < 2:
        return
    prev, cur = bars[-2], bars[-1]

    is_bull = cur.low < prev.low and cur.close > prev.high
    is_bear = cur.high > prev.high and cur.close < prev.low
    if not (is_bull or is_bear):
        return
    if params.use_doji_filter and not _is_doji(
        prev.open, prev.high, prev.low, prev.close, params.doji_body_ratio
    ):
        return
    if params.use_ma_filter:
        sma_v = _sma([b.close for b in bars], params.sma_length)
        if sma_v is None:
            return
        if is_bull and not (cur.close > sma_v):
            return
        if is_bear and not (cur.close < sma_v):
            return

    entry = cur.close
    buffer = _atr(bars, params.atr_length) * params.atr_fraction
    if is_bull:
        sl_price = cur.low - buffer
        tp_price = entry + abs(entry - sl_price) * params.rr_target
    else:
        sl_price = cur.high + buffer
        tp_price = entry - abs(sl_price - entry) * params.rr_target
    if abs(entry - sl_price) <= 0 or abs(tp_price - entry) <= 0:
        return

    open_ts = signals.bar_open_ts(engine, bar)
    label = signals.session_label(engine, open_ts, NAME)
    signals.stage(
        engine,
        strategy=NAME,
        session=label or NAME,
        side="long" if is_bull else "short",
        ref_entry=entry,
        sl_price=sl_price,
        tp_price=tp_price,
        signal_ts=bar.ts,
        anchor_to_fill=False,
        detail={"trigger": "fu", "swept_level": prev.low if is_bull else prev.high},
    )
