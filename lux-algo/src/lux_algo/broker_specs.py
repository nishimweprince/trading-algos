"""Published broker contract specifications, keyed by MT5 symbol.

These are **published** specs, not values read from the terminal. mt5-trader exposes no
symbol-info endpoint (only ``/v1/market-data/candles`` and ``/v1/market-data/tick``), so
position sizing has to be computed against a table maintained here.

``contract_size`` is the number of quote units one lot controls, which for a USD-quoted
instrument makes the cash value of a one-lot, one-price-unit move exactly
``contract_size`` dollars. Deriv's synthetic indices are quoted in USD with a contract
size of 1, so 1.00 lot moving 1.00 in price is $1. XAUUSD is 100 ounces, so its $2.50
stop (25 pips at ``pip_size`` 0.10) costs $250 per lot.

``volume_min`` / ``volume_step`` / ``volume_max`` mirror the broker's volume grid.
mt5-trader's ``_validate_volume`` **rejects** a signal with 422 when the volume is off
that grid rather than rounding it, so calibrated lots must land on ``volume_min + n *
volume_step`` exactly.

Re-check this table against the terminal (MT5 -> Symbols -> Specification) whenever Deriv
changes contract specs, when a symbol is added, or when a calibration run reports a
volume the broker then rejects. The per-symbol minimums here were cross-checked against
the volumes previously hand-set in ``symbols.example.deriv.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BrokerSpec:
    """Contract and volume grid for one MT5 symbol."""

    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal

    def usd_per_price_unit(self, volume: Decimal) -> Decimal:
        """Cash value of a 1.00 price move at ``volume`` lots (USD-quoted symbols)."""
        return volume * self.contract_size


def _deriv(volume_min: str, volume_max: str = "100") -> BrokerSpec:
    """Deriv synthetic: USD-quoted, contract size 1, 0.01 volume step."""
    return BrokerSpec(
        contract_size=Decimal("1"),
        volume_min=Decimal(volume_min),
        volume_step=Decimal("0.01"),
        volume_max=Decimal(volume_max),
    )


BROKER_SPECS: dict[str, BrokerSpec] = {
    # --- Deriv synthetic indices (DMT5) ---
    "Volatility 10 Index": _deriv("0.50", "200"),
    "Volatility 25 Index": _deriv("0.50"),
    "Volatility 50 Index": _deriv("4.00"),
    "Volatility 75 Index": _deriv("0.01", "30"),
    "Volatility 100 Index": _deriv("0.50"),
    "Crash 300 Index": _deriv("0.50"),
    "Crash 500 Index": _deriv("0.20"),
    "Crash 1000 Index": _deriv("0.20"),
    "Boom 300 Index": _deriv("0.50"),
    "Boom 500 Index": _deriv("0.20"),
    "Boom 1000 Index": _deriv("0.20"),
    "Step Index": _deriv("0.10"),
    "Step Index 200": _deriv("0.10"),
    "Step Index 500": _deriv("0.10"),
    # --- Forex / crypto (contract size in quote units per lot) ---
    "XAUUSD": BrokerSpec(
        contract_size=Decimal("100"),  # troy ounces
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("50"),
    ),
    "BTCUSD": BrokerSpec(
        contract_size=Decimal("1"),
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("10"),
    ),
}


def lookup_spec(mt5_symbol: str) -> BrokerSpec | None:
    return BROKER_SPECS.get(mt5_symbol)
