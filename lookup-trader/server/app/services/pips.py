"""Pip sizing. Mirrors client/src/lib/pips.ts — keep the two in step."""

from __future__ import annotations

from app.config import settings


def pip_size(symbol: str, price_digits: int = 5) -> float:
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if s == "XAUUSD":
        return 0.1
    if s == "XAGUSD":
        return 0.01
    if s.endswith("USD") or s.startswith(("EUR", "GBP", "AUD")):
        return 0.0001
    return 10.0**-price_digits


def spread_pips(symbol: str) -> float:
    return settings.spread_pips.get(symbol.upper(), settings.default_spread_pips)


def net_r(realized_r: float | None, symbol: str, entry: float, sl: float) -> float | None:
    """Realized R less the assumed round-trip spread. Never changes the label."""
    if realized_r is None:
        return None
    cost = spread_pips(symbol) * pip_size(symbol)
    risk = abs(entry - sl) or 1e-9
    return realized_r - cost / risk
