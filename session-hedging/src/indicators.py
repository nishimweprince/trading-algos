"""True range, Wilder ATR14, and the frozen 50/50 opening-range blend."""

from __future__ import annotations

from models import Candle

ATR14_PERIOD = 14
ORB_ATR14_BLEND_ORB_WEIGHT = 0.5
ATR_HISTORY_BARS = 64


def true_range(bar: Candle, prev_close: float) -> float:
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


def wilder_atr(bars: list[Candle], period: int = ATR14_PERIOD) -> float | None:
    """Wilder ATR of completed bars. Needs ``period + 1`` bars (prior close plus ``period`` TRs)."""
    if period <= 0 or len(bars) < period + 1:
        return None
    trs = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def blended_orb_atr(range_price: float, atr: float) -> float:
    """50/50 opening-range and ATR14 estimator, before ``SL_MULT``."""
    atr_weight = 1.0 - ORB_ATR14_BLEND_ORB_WEIGHT
    return ORB_ATR14_BLEND_ORB_WEIGHT * range_price + atr_weight * atr
