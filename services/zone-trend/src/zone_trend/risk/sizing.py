"""Position size from percentage price movement, then exchange filters.

``movement`` (default)::

    notional = min(r * E * k_sym / (d + 2f + s),  m * B * L) * size_mult

``fixed_margin``::

    notional = margin_pct * B * L * size_mult

r risk %, E equity, d stop % (|entry - stop| / entry), f taker fee, s
slippage, m max margin %, B available balance, L leverage, k_sym the
displacement multiplier, size_mult the breaker multiplier (0.5 when halved).

The spec writes ``min(...) * k_sym``; here ``k_sym`` scales the risk budget
inside the cap, so a large multiplier can never push margin past
``max_margin_pct``.

Quantities are rounded down to ``stepSize``. When the TP1 slice or the
remainder would be under 1.1 x minNotional, the position uses a single
take-profit at TP1 for the full quantity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from zone_trend.config import Exchange, Sizing

__all__ = ["DEFAULT_FILTERS", "Filters", "Size", "notional_for", "size_order", "stop_pct"]


@dataclass(frozen=True)
class Filters:
    tick: float
    step: float
    min_qty: float
    min_notional: float


# For symbols with no exchangeInfo row (delisted before the snapshot): no
# rounding beyond 1e-8 and Binance's common $5 minimum notional.
DEFAULT_FILTERS = Filters(tick=0.0, step=1e-8, min_qty=0.0, min_notional=5.0)


@dataclass(frozen=True)
class Size:
    qty: float
    notional: float
    tp1_qty: float  # == qty when single_tp
    single_tp: bool
    risk_usd: float  # loss at the stop including round-trip taker fees and slippage


def stop_pct(entry: float, stop: float) -> float:
    return abs(entry - stop) / entry


def notional_for(
    d: float,
    equity: float,
    available: float,
    sizing: Sizing,
    exchange: Exchange,
    k_sym: float = 1.0,
    size_mult: float = 1.0,
) -> float:
    lev = exchange.leverage
    if sizing.mode == "fixed_margin":
        return sizing.margin_pct * available * lev * size_mult
    cost = d + 2 * exchange.fee_taker + sizing.slippage_pct
    raw = sizing.risk_pct * equity * k_sym / cost
    cap = sizing.max_margin_pct * available * lev
    return min(raw, cap) * size_mult


def _floor_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-9) * step


def size_order(
    entry: float,
    stop: float,
    equity: float,
    available: float,
    sizing: Sizing,
    exchange: Exchange,
    filters: Filters,
    tp1_fraction: float,
    k_sym: float = 1.0,
    size_mult: float = 1.0,
) -> Size | None:
    """The order size, or None when it falls under the exchange minimums."""
    d = stop_pct(entry, stop)
    notional = notional_for(d, equity, available, sizing, exchange, k_sym, size_mult)
    qty = _floor_step(notional / entry, filters.step)
    if qty <= 0 or qty < filters.min_qty or qty * entry < filters.min_notional:
        return None
    tp1_qty = _floor_step(qty * tp1_fraction, filters.step)
    floor = 1.1 * filters.min_notional
    single = tp1_qty * entry < floor or (qty - tp1_qty) * entry < floor
    if single:
        tp1_qty = qty
    notional = qty * entry
    risk = notional * (d + 2 * exchange.fee_taker + sizing.slippage_pct)
    return Size(qty, notional, tp1_qty, single, risk)
