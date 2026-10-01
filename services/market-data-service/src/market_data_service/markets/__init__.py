"""Market modules: which providers may serve which market.

Policy only. Nothing here talks to a broker; each module names the providers a
market may be bound to, and ``validate_binding`` enforces it at startup so a
misconfigured profile fails before it serves a single quote.
"""

from __future__ import annotations

from ta_contracts import MarketKind

from . import crypto, deriv, forex

__all__ = ["POLICIES", "validate_binding"]

POLICIES: dict[MarketKind, frozenset[str]] = {
    MarketKind.FOREX: forex.ALLOWED_PROVIDERS,
    MarketKind.DERIV: deriv.ALLOWED_PROVIDERS,
    MarketKind.CRYPTO: crypto.ALLOWED_PROVIDERS,
}


def validate_binding(market: MarketKind, provider: str) -> None:
    allowed = POLICIES[market]
    if provider not in allowed:
        raise ValueError(
            f"market {market.value} cannot be served by {provider!r}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
