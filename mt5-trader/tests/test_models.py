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


@pytest.mark.parametrize(
    "source", ["trading_central", "autochartist", "lux_algo", "ipda", "custom_bot"]
)
def test_accepted_signal_sources(source: str) -> None:
    assert SignalRequest.model_validate(base_payload() | {"source": source}).source == source


def test_source_is_normalized_to_lowercase_slug() -> None:
    assert SignalRequest.model_validate(base_payload() | {"source": "IPDA"}).source == "ipda"


def test_invalid_source_slug_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalRequest.model_validate(base_payload() | {"source": "Trading Central"})


def test_absolute_and_distance_targets_are_mutually_exclusive() -> None:
    for absolute, distance in (
        ("stop_loss", "stop_loss_distance"),
        ("take_profit", "take_profit_distance"),
    ):
        with pytest.raises(ValidationError):
            SignalRequest.model_validate(
                base_payload() | {absolute: "1.09900", distance: "0.00100"}
            )


def test_canonical_json_is_unchanged_when_distances_are_unset() -> None:
    """The canonical form feeds the idempotency hash. Adding the distance fields must not
    change the hash of payloads written before those fields existed."""
    payload = base_payload() | {"stop_loss": "1.09900", "take_profit": "1.10200"}
    canonical = SignalRequest.model_validate(payload).canonical_json()

    assert "stop_loss_distance" not in canonical
    assert "take_profit_distance" not in canonical


def test_canonical_json_includes_distances_when_set() -> None:
    payload = base_payload() | {"stop_loss_distance": "0.00100"}
    canonical = SignalRequest.model_validate(payload).canonical_json()

    assert "stop_loss_distance" in canonical
    assert "take_profit_distance" not in canonical
