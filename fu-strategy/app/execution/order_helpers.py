"""Order-placement helpers extracted from vrvp-strategy LiveExecutionEngine.

Used to:
  - derive price decimal precision from Capital.com market metadata
  - read broker-reported minimum deal size
  - round SL/TP away from entry to satisfy minimum-distance rules

The full LiveExecutionEngine class is wired in step 9 once the risk + signal
modules exist. These helpers stand alone and can be used by the paper-mode
logger in the meantime.
"""
import math
from typing import Dict, Optional

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
