"""Maps a strategy Decision to the mt5-trader SignalRequest payload.

Market orders MUST NOT include entry_price or expires_at (mt5-trader's model validator
rejects them). ``source`` is "lux_algo", which must be added to mt5-trader's
SignalSource enum. Prices are quantized to PRICE_DIGITS so mt5-trader's price-precision
check passes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import Settings
from .signal_gate import signal_id_for
from .strategy import Decision

SOURCE = "lux_algo"


def _price(value: float, digits: int) -> Decimal:
    quant = Decimal(1).scaleb(-digits)  # e.g. digits=5 -> 0.00001
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


def build_signal_payload(
    decision: Decision, settings: Settings, occurred_at: datetime | None = None
) -> dict[str, Any]:
    occurred = occurred_at or datetime.now(UTC)
    signal_id = signal_id_for(settings.mt5_symbol, decision.bucket_start, decision.direction)

    payload: dict[str, Any] = {
        "signal_id": str(signal_id),
        "occurred_at": occurred.isoformat(),
        "execution_type": "market",
        "symbol": settings.mt5_symbol,
        "direction": decision.direction,
        "volume": str(settings.volume),
        "source": SOURCE,
        "ignore_signal_age": settings.ignore_signal_age,
    }
    if decision.stop_loss is not None:
        payload["stop_loss"] = str(_price(decision.stop_loss, settings.price_digits))
    if decision.take_profit is not None:
        payload["take_profit"] = str(_price(decision.take_profit, settings.price_digits))
    if settings.deviation_points is not None:
        payload["deviation_points"] = settings.deviation_points
    payload["note"] = (
        f"lux-algo supertrend {decision.direction} @ {decision.bucket_start.isoformat()}"
    )
    return payload
