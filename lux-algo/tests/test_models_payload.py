from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from lux_algo.config import Settings
from lux_algo.models import build_signal_payload
from lux_algo.strategy import Decision


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "data_api_url": "https://data.example.com/candles",
        "quote": "EURUSD",
        "mt5_symbol": "EURUSD",
        "volume": Decimal("0.10"),
        "mt5_signal_api_key": "unit-test-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _decision(direction: str = "buy") -> Decision:
    return Decision(
        direction=direction,
        bucket_start=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
        entry=1.234567,
        stop_loss=1.230001,
        take_profit=1.243700,
        supertrend=1.230001,
    )


def test_market_payload_has_no_entry_or_expiry_and_lux_algo_source() -> None:
    payload = build_signal_payload(_decision(), _settings())
    assert payload["execution_type"] == "market"
    assert payload["source"] == "lux_algo"
    assert "entry_price" not in payload
    assert "expires_at" not in payload
    assert payload["direction"] == "buy"
    assert payload["volume"] == "0.10"


def test_prices_are_quantized_to_price_digits() -> None:
    payload = build_signal_payload(_decision(), _settings(price_digits=5))
    assert payload["stop_loss"] == "1.23000"
    assert payload["take_profit"] == "1.24370"


def test_toggles_omit_sl_tp() -> None:
    payload = build_signal_payload(
        Decision(
            direction="buy",
            bucket_start=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
            entry=1.2,
            stop_loss=None,
            take_profit=None,
            supertrend=1.19,
        ),
        _settings(),
    )
    assert "stop_loss" not in payload
    assert "take_profit" not in payload


def test_signal_id_is_stable_across_builds() -> None:
    a = build_signal_payload(_decision(), _settings())["signal_id"]
    b = build_signal_payload(_decision(), _settings())["signal_id"]
    assert a == b
