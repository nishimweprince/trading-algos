"""Published broker contract specifications, keyed by MT5 symbol."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BrokerSpec:
    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal

    def usd_per_price_unit(self, volume: Decimal) -> Decimal:
        return volume * self.contract_size


def _deriv(volume_min: str, volume_max: str = "100") -> BrokerSpec:
    return BrokerSpec(
        contract_size=Decimal("1"),
        volume_min=Decimal(volume_min),
        volume_step=Decimal("0.01"),
        volume_max=Decimal(volume_max),
    )


BROKER_SPECS: dict[str, BrokerSpec] = {
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
    "XAUUSD": BrokerSpec(
        contract_size=Decimal("100"),
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
