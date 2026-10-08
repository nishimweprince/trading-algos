"""Polymarket's taker fee, always from the fill price (kpc trap 1).

Per share it is ``rate * (p * (1 - p)) ** exponent``, in USDC; makers pay
nothing. Crypto 15-minute markets carry ``rate = 0.07, exponent = 1`` in their
Gamma ``feeSchedule`` (checked 2026-10-06), i.e. ``700 * (1 - p)`` bps of
notional: 350 bps at 0.50, 200 bps at ~0.714. A flat bps constant is wrong
everywhere except one price.
"""

from __future__ import annotations

__all__ = ["DEFAULT_EXPONENT", "DEFAULT_RATE", "fee_bps", "taker_fee"]

DEFAULT_RATE = 0.07
DEFAULT_EXPONENT = 1.0


def taker_fee(
    price: float, shares: float, rate: float = DEFAULT_RATE, exponent: float = DEFAULT_EXPONENT
) -> float:
    if shares <= 0 or not 0 < price < 1:
        return 0.0
    return shares * rate * (price * (1 - price)) ** exponent


def fee_bps(price: float, rate: float = DEFAULT_RATE, exponent: float = DEFAULT_EXPONENT) -> float:
    """The fee as basis points of notional (``price * shares``)."""
    return 1e4 * taker_fee(price, 1.0, rate, exponent) / price if 0 < price < 1 else 0.0
