"""Order-placement helpers extracted from vrvp-strategy LiveExecutionEngine.

Used to:
  - derive price decimal precision from Capital.com market metadata
  - read broker-reported minimum deal size and minimum stop distance
  - read live bid/offer from the market snapshot
  - round SL/TP away from entry to satisfy minimum-distance rules
  - push SL/TP out so they sit on the correct side of market price and
    clear the broker's minimum-distance rule plus the current spread

The full LiveExecutionEngine class is wired in step 9 once the risk + signal
modules exist. These helpers stand alone and can be used by the paper-mode
logger in the meantime.
"""
import math
from typing import Dict, Optional, Tuple

from loguru import logger

from app.data.instrument_specs import InstrumentSpec


def get_decimal_places(market_info: Dict, spec: Optional[InstrumentSpec] = None) -> int:
    """Decimal precision for SL/TP. Prefers Capital.com scalingFactor, falls back to tick_size."""
    snapshot = market_info.get('snapshot', {})
    scaling_factor = snapshot.get('scalingFactor', 1)
    if scaling_factor and scaling_factor > 1:
        decimals = max(0, round(math.log10(scaling_factor)))
        logger.debug(f"Decimal places from API scalingFactor={scaling_factor}: {decimals}")
        return decimals

    if spec and spec.tick_size > 0 and spec.tick_size < 1:
        decimals = max(0, round(-math.log10(spec.tick_size)))
        logger.debug(f"Decimal places from spec tick_size={spec.tick_size}: {decimals}")
        return decimals

    return 5


def get_min_deal_size(market_info: Dict) -> Optional[float]:
    """Broker-reported minimum deal size, or None if not provided."""
    dealing_rules = market_info.get('dealingRules', {})
    min_deal = dealing_rules.get('minDealSize', {})
    value = min_deal.get('value')
    if value and value > 0:
        return float(value)
    return None


def round_stop_loss(price: float, decimals: int, direction: str) -> float:
    """Round SL away from entry so the resulting level is at least `1/10**decimals` from entry."""
    factor = 10 ** decimals
    if direction == 'BUY':
        return math.floor(price * factor) / factor
    return math.ceil(price * factor) / factor


def round_take_profit(price: float, decimals: int, direction: str) -> float:
    """Round TP away from entry."""
    factor = 10 ** decimals
    if direction == 'BUY':
        return math.ceil(price * factor) / factor
    return math.floor(price * factor) / factor


def get_min_stop_distance(market_info: Dict, guaranteed: bool = False,
                          reference_price: Optional[float] = None) -> Optional[float]:
    """Broker-reported minimum stop/limit distance in price units (POINTS).

    For PERCENTAGE-unit rules (common on crypto/indices), `reference_price`
    is multiplied in to give an absolute distance. Returns None only if no
    usable rule is found.
    """
    rules = market_info.get('dealingRules', {})
    candidates = []
    if guaranteed:
        candidates.append(rules.get('minControlledRiskStopDistance'))
    candidates.append(rules.get('minNormalStopOrLimitDistance'))
    candidates.append(rules.get('minStepDistance'))
    for rule in candidates:
        if not rule:
            continue
        value = rule.get('value')
        unit = (rule.get('unit') or 'POINTS').upper()
        if value is None or value <= 0:
            continue
        if unit in ('POINTS', 'POINT'):
            return float(value)
        if unit == 'PERCENTAGE' and reference_price:
            return float(value) / 100.0 * float(reference_price)
    return None


def get_market_prices(market_info: Dict) -> Optional[Tuple[float, float]]:
    """Returns (bid, offer) from the market snapshot, or None if unavailable."""
    snap = market_info.get('snapshot', {})
    bid = snap.get('bid')
    offer = snap.get('offer')
    if bid is None or offer is None:
        return None
    return float(bid), float(offer)


def adjust_levels_for_broker(direction: str, sl: float, tp: float, *,
                             bid: float, offer: float,
                             min_distance: float,
                             buffer_multiplier: float = 1.5) -> Tuple[float, float]:
    """Push SL/TP onto the correct side of market and clear broker minimums.

    For BUY  (entry at offer): SL must be below bid, TP above offer.
    For SELL (entry at bid):   SL must be above offer, TP below bid.
    The required clearance is `min_distance * buffer_multiplier` plus the
    current spread, so a tick of adverse movement does not invalidate the
    order. Levels already further away are kept as-is.
    """
    spread = max(0.0, offer - bid)
    safety = (min_distance * buffer_multiplier) + spread
    if direction.upper() == 'BUY':
        max_sl = bid - safety
        sl = min(sl, max_sl)
        min_tp = offer + safety
        tp = max(tp, min_tp)
    else:
        min_sl = offer + safety
        sl = max(sl, min_sl)
        max_tp = bid - safety
        tp = min(tp, max_tp)
    return sl, tp
