"""USDⓈ-M contract specifications from ``GET /fapi/v1/exchangeInfo``."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from ta_contracts import InstrumentInfo
from ta_core import ServiceError

from .rest import FapiRest

__all__ = ["PROVIDER_NAME", "WEIGHT_EXCHANGE_INFO", "digits", "load_instruments"]

PROVIDER_NAME = "binance_futures"
WEIGHT_EXCHANGE_INFO = 1


def digits(tick_size: str) -> int:
    exponent = Decimal(tick_size).normalize().as_tuple().exponent
    return max(0, -int(exponent))


async def load_instruments(rest: FapiRest, symbols: Sequence[str]) -> dict[str, InstrumentInfo]:
    """Perpetuals only: a symbol that is not a trading PERPETUAL is refused."""
    payload = await rest.get(
        "/fapi/v1/exchangeInfo", {}, WEIGHT_EXCHANGE_INFO, unavailable="broker_not_ready"
    )
    found = {
        row["symbol"]: row
        for row in payload.get("symbols", [])
        if row.get("contractType") == "PERPETUAL" and row.get("status") == "TRADING"
    }
    missing = sorted(set(symbols) - set(found))
    if missing:
        raise ServiceError(
            503,
            "broker_not_ready",
            f"Binance USDⓈ-M does not list trading perpetuals {missing}",
            {"missing": missing},
        )
    instruments: dict[str, InstrumentInfo] = {}
    for symbol in symbols:
        row = found[symbol]
        filters = {item["filterType"]: item for item in row.get("filters", [])}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        tick = price.get("tickSize")
        instruments[symbol] = InstrumentInfo(
            symbol=symbol,
            source_instrument=symbol,
            provider=PROVIDER_NAME,
            digits=digits(tick) if tick else int(row.get("pricePrecision", 8)),
            description=f"{row.get('baseAsset')}/{row.get('quoteAsset')} perpetual",
            price_increment=float(tick) if tick else None,
            quantity_increment=float(lot["stepSize"]) if "stepSize" in lot else None,
            min_quantity=float(lot["minQty"]) if "minQty" in lot else None,
            max_quantity=float(lot["maxQty"]) if "maxQty" in lot else None,
        )
    return instruments
