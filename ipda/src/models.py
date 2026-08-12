"""Maps a strategy Decision to the mt5-trader SignalRequest payload.

Market orders MUST NOT include entry_price or expires_at. ``source`` is "ipda".
Hard targets travel as distances so mt5-trader anchors them to the fill price.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import Settings
from .instruments import InstrumentConfig
from .signal_gate import signal_id_for
from .strategy import Decision

SOURCE = "ipda"


def _price(value: float, digits: int) -> Decimal:
    quant = Decimal(1).scaleb(-digits)
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


def build_signal_payload(
    decision: Decision,
    instrument: InstrumentConfig,
    settings: Settings,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    occurred = occurred_at or datetime.now(UTC)
    symbol = instrument.resolved_mt5_symbol()
    price_digits = instrument.resolved_price_digits(settings)
    signal_id = signal_id_for(symbol, decision.bucket_start, decision.direction)

    payload: dict[str, Any] = {
        "signal_id": str(signal_id),
        "occurred_at": occurred.isoformat(),
        "execution_type": "market",
        "symbol": symbol,
        "direction": decision.direction,
        "volume": str(instrument.resolved_volume(settings)),
        "source": SOURCE,
        "ignore_signal_age": settings.ignore_signal_age,
    }
    if decision.stop_loss is not None:
        payload["stop_loss"] = str(_price(decision.stop_loss, price_digits))
    if decision.take_profit is not None:
        payload["take_profit"] = str(_price(decision.take_profit, price_digits))
    if decision.stop_loss_distance is not None:
        payload["stop_loss_distance"] = str(
            _price(decision.stop_loss_distance, price_digits)
        )
    if decision.take_profit_distance is not None:
        payload["take_profit_distance"] = str(
            _price(decision.take_profit_distance, price_digits)
        )
    deviation_points = instrument.resolved_deviation_points(settings)
    if deviation_points is not None:
        payload["deviation_points"] = deviation_points
    payload["note"] = (
        f"ipda supertrend {decision.direction} @ {decision.bucket_start.isoformat()}"
    )
    return payload
