from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from ta_contracts import (
    Candle,
    Direction,
    ExecutionType,
    LegacyCandle,
    OperationState,
    OrderRequest,
    SignalRequest,
    Tick,
    TimeInForce,
)

SIGNAL_ID = UUID("11111111-2222-3333-4444-555555555555")
OCCURRED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def market_signal(**overrides: object) -> SignalRequest:
    payload: dict[str, object] = {
        "signal_id": SIGNAL_ID,
        "occurred_at": OCCURRED,
        "execution_type": ExecutionType.MARKET,
        "symbol": "XAUUSD",
        "direction": Direction.BUY,
        "volume": Decimal("0.10"),
        "source": "lux_algo",
    }
    payload.update(overrides)
    return SignalRequest(**payload)  # type: ignore[arg-type]


# --- idempotency ------------------------------------------------------------
#
# canonical_json feeds the hash that gates replay against the existing
# signals.db. These are golden values: if one changes, already-filled signals
# re-execute.


def test_canonical_json_omits_unset_distance_fields() -> None:
    """Payloads written before the distance fields existed must hash the same."""
    assert "stop_loss_distance" not in market_signal().canonical_json()
    assert "take_profit_distance" not in market_signal().canonical_json()


def test_canonical_json_keeps_a_set_distance_field() -> None:
    body = market_signal(stop_loss_distance=Decimal("1.5")).canonical_json()
    assert "stop_loss_distance" in body
    assert "take_profit_distance" not in body


def test_canonical_json_keeps_other_unset_fields_as_null() -> None:
    """exclude_none=False: only the two distances are conditional."""
    assert '"entry_price":null' in market_signal().canonical_json()


def test_canonical_json_hash_is_stable() -> None:
    """Golden value, derived by running mt5-trader's original SignalRequest.

    Verified byte-identical against mt5-trader/src/mt5_signal_service/models.py
    across market/limit, absolute-stop, distance and note payloads at migration
    time. It is pinned here so a later edit to the model cannot silently change
    the replay hash for signals already recorded in signals.db.
    """
    digest = hashlib.sha256(market_signal().canonical_json().encode()).hexdigest()
    assert digest == "c8dcc64694b48918fc5ab977b8aa09a297aba74226ed0797b0c9126dd007046d"


def test_identical_payloads_hash_identically() -> None:
    assert market_signal().canonical_json() == market_signal().canonical_json()


def test_differing_payloads_hash_differently() -> None:
    other = market_signal(volume=Decimal("0.20"))
    assert market_signal().canonical_json() != other.canonical_json()


# --- signal validation ------------------------------------------------------


def test_market_order_rejects_entry_price() -> None:
    with pytest.raises(ValidationError, match="entry_price is prohibited"):
        market_signal(entry_price=Decimal("2000"))


def test_limit_order_requires_entry_price() -> None:
    with pytest.raises(ValidationError, match="entry_price is required"):
        market_signal(execution_type=ExecutionType.LIMIT)


def test_stop_loss_and_distance_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        market_signal(stop_loss=Decimal("1990"), stop_loss_distance=Decimal("10"))


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        market_signal(occurred_at=datetime(2026, 1, 2, 3, 4, 5))


def test_source_is_lowercased() -> None:
    assert market_signal(source="  LUX_ALGO  ").source == "lux_algo"


# --- operations -------------------------------------------------------------


def order_request(**overrides: object) -> OrderRequest:
    payload: dict[str, object] = {
        "operation_id": SIGNAL_ID,
        "occurred_at": OCCURRED,
        "source": "session_hedging",
        "instrument": "XAUUSD",
        "execution_type": ExecutionType.MARKET,
        "direction": Direction.BUY,
        "targets": [{"account": "forex-demo", "volume_lots": Decimal("0.1")}],
    }
    payload.update(overrides)
    return OrderRequest(**payload)  # type: ignore[arg-type]


def test_order_instrument_is_uppercased() -> None:
    assert order_request(instrument=" xauusd ").instrument == "XAUUSD"


def test_targets_must_not_repeat_an_account() -> None:
    duplicate = [
        {"account": "forex-demo", "volume_lots": Decimal("0.1")},
        {"account": "forex-demo", "volume_lots": Decimal("0.2")},
    ]
    with pytest.raises(ValidationError, match="repeat an account alias"):
        order_request(targets=duplicate)


def test_market_order_prohibits_gtd() -> None:
    with pytest.raises(ValidationError, match="GTD"):
        order_request(time_in_force=TimeInForce.GTD)


def test_gtd_requires_an_expiry() -> None:
    with pytest.raises(ValidationError, match="GTD orders require expires_at"):
        order_request(
            execution_type=ExecutionType.LIMIT,
            entry_price=Decimal("2000"),
            time_in_force=TimeInForce.GTD,
        )


def test_operation_states_cover_partial_failure() -> None:
    """A fan-out across accounts can half-succeed; the state model must say so."""
    assert OperationState.PARTIAL_FAILURE in set(OperationState)


# --- market data ------------------------------------------------------------


def test_candle_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Candle(
            ts=datetime(2026, 1, 2),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            source_instrument="XAUUSD",
        )


def test_candle_and_legacy_candle_are_deliberately_different() -> None:
    """mt5-trader's /v1/candles shape is not the canonical one. Guard the split."""
    assert "ts" in Candle.model_fields and "time" not in Candle.model_fields
    assert "time" in LegacyCandle.model_fields and "ts" not in LegacyCandle.model_fields
    assert "provider" in Candle.model_fields
    assert "provider" not in LegacyCandle.model_fields


def test_tick_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Tick(
            symbol="XAUUSD",
            bid=1.0,
            ask=1.1,
            spread=0.1,
            ts=datetime.now(UTC),
            surprise=True,
        )
