from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mt5_signal_service.models import SignalRequest


def base_payload() -> dict[str, object]:
    return {
        "signal_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "execution_type": "market",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": "0.10",
        "source": "trading_central",
    }


@pytest.mark.parametrize(
    "field",
    ["signal_id", "occurred_at", "execution_type", "symbol", "direction", "volume", "source"],
)
def test_required_fields(field: str) -> None:
    payload = base_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        SignalRequest.model_validate(payload)


def test_market_prohibits_entry_and_expiration() -> None:
    payload = base_payload() | {
        "entry_price": "1.1",
        "expires_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ValidationError, match="entry_price is prohibited"):
        SignalRequest.model_validate(payload)


@pytest.mark.parametrize("execution_type", ["limit", "stop"])
def test_pending_requires_entry_price(execution_type: str) -> None:
    with pytest.raises(ValidationError, match="entry_price is required"):
        SignalRequest.model_validate(base_payload() | {"execution_type": execution_type})


def test_unknown_fields_and_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalRequest.model_validate(base_payload() | {"credentials": "secret"})
    with pytest.raises(ValidationError, match="timezone"):
        SignalRequest.model_validate(base_payload() | {"occurred_at": "2026-01-01T12:00:00"})


def test_sl_and_tp_are_independently_optional() -> None:
    assert SignalRequest.model_validate(base_payload()).stop_loss is None
    assert SignalRequest.model_validate(base_payload() | {"stop_loss": "1.09"}).take_profit is None
