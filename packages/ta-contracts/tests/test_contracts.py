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
    MarketQuote,
    OperationState,
    OrderRequest,
    SignalRequest,
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


def test_quote_with_both_sides_derives_mid_price_and_spread() -> None:
    quote = MarketQuote(
        symbol="XAUUSD",
        source_instrument="XAUUSDb",
        provider="mt5",
        ts=datetime.now(UTC),
        bid=2000.0,
        ask=2000.5,
    )

    assert quote.price == pytest.approx(2000.25)
    assert quote.spread == pytest.approx(0.5)


def test_quote_without_bid_ask_needs_an_explicit_price() -> None:
    with pytest.raises(ValidationError, match="price"):
        MarketQuote(
            symbol="BTCUSDT",
            source_instrument="BTCUSDT",
            provider="binance",
            ts=datetime.now(UTC),
        )

    last_trade = MarketQuote(
        symbol="BTCUSDT",
        source_instrument="BTCUSDT",
        provider="binance",
        ts=datetime.now(UTC),
        price=60000.0,
    )
    assert last_trade.bid is None and last_trade.spread is None


def test_quote_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        MarketQuote(
            symbol="XAUUSD",
            source_instrument="XAUUSD",
            provider="ctrader",
            ts=datetime(2026, 1, 2),
            bid=1.0,
            ask=1.1,
        )


def test_quote_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MarketQuote(
            symbol="XAUUSD",
            source_instrument="XAUUSD",
            provider="ctrader",
            ts=datetime.now(UTC),
            bid=1.0,
            ask=1.1,
            surprise=True,
        )


def test_oco_group_request_accepts_account_and_hashes_as_before() -> None:
    import hashlib

    from ta_contracts import OcoGroupRequest

    payload = {
        "group_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "occurred_at": "2026-09-01T10:00:00+00:00",
        "decision_at": "2026-09-01T10:00:00+00:00",
        "symbol": "EURUSD",
        "volume": "0.1",
        "upper_trigger": "1.1010",
        "lower_trigger": "1.0990",
        "stop_distance": "0.001",
        "target_distance": "0.002",
        "expires_at": "2026-09-01T10:15:00+00:00",
        "source": "trading_central",
    }
    by_profile = OcoGroupRequest.model_validate({**payload, "profile": "hfm"})
    by_account = OcoGroupRequest.model_validate({**payload, "account": "hfm"})

    assert by_profile == by_account and by_account.account == "hfm"
    dumped = by_account.model_dump_json()
    assert '"account"' not in dumped
    # Pinned: stored groups replay against this hash.
    assert (
        hashlib.sha256(dumped.encode()).hexdigest()
        == hashlib.sha256(by_profile.model_dump_json().encode()).hexdigest()
    )
    assert dumped.startswith('{"group_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","profile":"hfm"')


# --- OrderRequest venue flags (post_only / reduce_only) --------------------------

GOLDEN_ORDER = {
    "operation_id": "6f2a1d3c-8b74-4e59-9a10-2f5c7d8e4b16",
    "occurred_at": "2026-10-02T10:00:00+00:00",
    "source": "ofi_scalper",
    "instrument": "btcusdt",
    "execution_type": "limit",
    "direction": "buy",
    "targets": [{"account": "binance_testnet", "volume_lots": "0.002"}],
    "entry_price": "60000.1",
    "stop_loss": "59990.0",
    "take_profit": "60010.5",
    "time_in_force": "gtc",
    "note": "golden",
}
# Captured from the code BEFORE post_only/reduce_only existed (2026-10-02).
GOLDEN_JSON = (
    '{"operation_id":"6f2a1d3c-8b74-4e59-9a10-2f5c7d8e4b16","occurred_at":"2026-10-02T10:00:00Z",'
    '"source":"ofi_scalper","instrument":"BTCUSDT","execution_type":"limit","direction":"buy",'
    '"targets":[{"account":"binance_testnet","volume_lots":"0.002"}],"entry_price":"60000.1",'
    '"stop_loss":"59990.0","take_profit":"60010.5","stop_loss_distance":null,'
    '"take_profit_distance":null,"time_in_force":"gtc","expires_at":null,"note":"golden"}'
)
GOLDEN_SHA256 = "1c0a96e7734153cb94856dce403148d2ada02fccfd80ae359ac0b6b04e2b2e07"


def test_order_without_flags_hashes_exactly_as_before() -> None:
    import hashlib

    from ta_contracts.execution import OrderRequest

    canonical = OrderRequest.model_validate(GOLDEN_ORDER).canonical_json()
    assert canonical == GOLDEN_JSON
    assert hashlib.sha256(canonical.encode()).hexdigest() == GOLDEN_SHA256


def test_set_flags_are_part_of_the_hash() -> None:
    from ta_contracts.execution import OrderRequest

    plain = OrderRequest.model_validate(GOLDEN_ORDER).canonical_json()
    maker = OrderRequest.model_validate({**GOLDEN_ORDER, "post_only": True}).canonical_json()
    exit_ = OrderRequest.model_validate({**GOLDEN_ORDER, "reduce_only": True}).canonical_json()
    assert '"post_only":true' in maker and '"reduce_only"' not in maker
    assert '"reduce_only":true' in exit_
    assert len({plain, maker, exit_}) == 3
    explicit_false = OrderRequest.model_validate({**GOLDEN_ORDER, "post_only": False})
    assert '"post_only":false' in explicit_false.canonical_json()


def test_post_only_is_limit_only() -> None:
    import pytest
    from pydantic import ValidationError

    from ta_contracts.execution import OrderRequest

    market = {k: v for k, v in GOLDEN_ORDER.items() if k != "entry_price"}
    market["execution_type"] = "market"
    with pytest.raises(ValidationError, match="post_only"):
        OrderRequest.model_validate({**market, "post_only": True})
    OrderRequest.model_validate({**market, "reduce_only": True})  # a market exit is fine
