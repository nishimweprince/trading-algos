"""Isolated-margin liquidation price (one-way mode) from a leverage bracket.

Binance's formula for a single isolated position::

    LP = (WB + cum - side * Q * EP) / (Q * MMR - side * Q)

WB is the isolated wallet balance (initial margin ``Q * EP / L`` here), cum the
bracket's maintenance amount, side +1 long / -1 short. Rule: the stop distance
must be at most half the distance to liquidation.

Leverage brackets need a signed request (fetched live in M5). Until then the
backtester uses ``DEFAULT_BRACKET``, a deliberately pessimistic 1% maintenance
margin rate (BTCUSDT's first bracket is 0.4%).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEFAULT_BRACKET", "Bracket", "liquidation_price", "stop_within_liq"]


@dataclass(frozen=True)
class Bracket:
    mmr: float
    cum: float = 0.0


DEFAULT_BRACKET = Bracket(mmr=0.01)


def liquidation_price(
    entry: float, qty: float, side: int, leverage: float, bracket: Bracket = DEFAULT_BRACKET
) -> float:
    wb = qty * entry / leverage
    return (wb + bracket.cum - side * qty * entry) / (qty * bracket.mmr - side * qty)


def stop_within_liq(
    entry: float, stop: float, side: int, leverage: float, bracket: Bracket = DEFAULT_BRACKET
) -> bool:
    liq = liquidation_price(entry, 1.0, side, leverage, bracket)
    return abs(entry - stop) <= 0.5 * abs(entry - liq)
