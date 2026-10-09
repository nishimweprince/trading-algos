from __future__ import annotations

import pytest
from pydantic import ValidationError

from ofi_scalper_service.config import ExecutionMode
from tests.conftest import make_settings


def test_defaults(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert settings.port == 8030
    assert settings.execution_mode is ExecutionMode.OFF
    assert settings.binance_futures_symbols == ("BTCUSDT", "ETHUSDT")
    assert settings.gate_vol_1m_bp is None


def test_live_mode_is_refused(tmp_path) -> None:
    with pytest.raises(ValidationError, match="live is refused"):
        make_settings(tmp_path, OFI_EXECUTION_MODE="live")


def test_shadow_mode_needs_a_model(tmp_path) -> None:
    with pytest.raises(ValidationError, match="shadow needs OFI_MODEL_VERSION"):
        make_settings(tmp_path, OFI_EXECUTION_MODE="shadow")
    settings = make_settings(tmp_path, OFI_EXECUTION_MODE="shadow", OFI_MODEL_VERSION="v1")
    assert settings.execution_mode is ExecutionMode.SHADOW


def test_testnet_mode_needs_the_gateway_key(tmp_path) -> None:
    with pytest.raises(ValidationError, match="testnet needs EXECUTION_API_KEY"):
        make_settings(tmp_path, OFI_EXECUTION_MODE="testnet")
    # No model: controls only (kill, dead-man, flatten), never an entry.
    settings = make_settings(
        tmp_path, OFI_EXECUTION_MODE="testnet", EXECUTION_API_KEY="gateway-key-at-least-16"
    )
    assert settings.execution_mode is ExecutionMode.TESTNET and settings.model_version is None


def test_order_size_must_fit_the_frozen_limits(tmp_path) -> None:
    with pytest.raises(ValidationError, match="cannot exceed OFI_MAX_POSITION"):
        make_settings(tmp_path, OFI_ORDER_NOTIONAL_USD=600)
    with pytest.raises(ValidationError, match="above OFI_MANUAL_APPROVAL"):
        make_settings(tmp_path, OFI_ORDER_NOTIONAL_USD=450)


def test_position_cap_cannot_exceed_total(tmp_path) -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        make_settings(tmp_path, OFI_MAX_POSITION_NOTIONAL_USD=900)


def test_risk_limits_are_required(tmp_path) -> None:
    with pytest.raises(ValidationError, match="OFI_DAILY_LOSS_LIMIT_USD"):
        make_settings(tmp_path, OFI_DAILY_LOSS_LIMIT_USD=None)


def test_telegram_token_needs_admins(tmp_path) -> None:
    with pytest.raises(ValidationError, match="ADMIN_USER_IDS"):
        make_settings(tmp_path, OFI_TELEGRAM_BOT_TOKEN="1:abc")
    settings = make_settings(
        tmp_path, OFI_TELEGRAM_BOT_TOKEN="1:abc", OFI_TELEGRAM_ADMIN_USER_IDS="42, 7"
    )
    assert settings.telegram_admin_user_ids == frozenset({42, 7})


def test_blank_optionals_are_unset(tmp_path) -> None:
    settings = make_settings(
        tmp_path, OFI_GATE_SPREAD_PCTL="", OFI_MAKER_FEE_BP="", OFI_TELEGRAM_BOT_TOKEN=""
    )
    assert settings.gate_spread_pctl is None
    assert settings.maker_fee_bp is None
    assert settings.telegram_bot_token is None


def test_placeholder_secrets_are_refused(tmp_path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, BINANCE_FUTURES_API_KEY="replace-with-read-only-key")


def test_provider_is_discovered(tmp_path) -> None:
    make_settings(tmp_path).validate_provider()


def test_every_placeholder_check_survives_the_mixins(tmp_path) -> None:
    # Validators are methods: a same-named one in a mixin or subclass silently
    # replaces the base's. Each of these must still be refused.
    for field in ("API_KEY", "BINANCE_FUTURES_API_SECRET", "OFI_TELEGRAM_BOT_TOKEN"):
        overrides = {field: "replace-with-something-long-enough"}
        if field == "OFI_TELEGRAM_BOT_TOKEN":
            overrides["OFI_TELEGRAM_ADMIN_USER_IDS"] = "1"
        with pytest.raises(ValidationError):
            make_settings(tmp_path, **overrides)


def test_unwritable_record_dir_fails_with_a_fix(tmp_path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        settings = make_settings(tmp_path, OFI_RECORD_DIR=str(locked / "ofi" / "raw"))
        with pytest.raises(ValueError, match="sudo mkdir -p .*locked/ofi/raw"):
            settings.check_record_dir()
        # A not-yet-existing path under a writable parent is fine; the recorder creates it.
        make_settings(tmp_path, OFI_RECORD_DIR=str(tmp_path / "new" / "raw")).check_record_dir()
        make_settings(
            tmp_path, OFI_RECORD_DIR=str(locked / "x"), OFI_RECORD_ENABLED=False
        ).check_record_dir()
    finally:
        locked.chmod(0o700)


def test_shadow_mirror_needs_testnet_and_a_model(tmp_path) -> None:
    with pytest.raises(ValueError, match="OFI_SHADOW_MIRROR"):
        make_settings(tmp_path, OFI_SHADOW_MIRROR="true")
    with pytest.raises(ValueError, match="OFI_SHADOW_MIRROR"):
        make_settings(
            tmp_path,
            OFI_SHADOW_MIRROR="true",
            OFI_EXECUTION_MODE="testnet",
            EXECUTION_API_KEY="gateway-key-at-least-16",
        )
    settings = make_settings(
        tmp_path,
        OFI_SHADOW_MIRROR="true",
        OFI_EXECUTION_MODE="testnet",
        EXECUTION_API_KEY="gateway-key-at-least-16",
        OFI_MODEL_VERSION="rules-v1",
        OFI_ALLOW_UNGATED="true",
    )
    assert settings.shadow_mirror and settings.allow_ungated


def test_micro_live_needs_the_ack_a_model_and_stays_under_the_caps(tmp_path) -> None:
    from ofi_scalper_service.config import MICRO_LIVE_ACK

    base = {
        "OFI_EXECUTION_MODE": "micro_live",
        "EXECUTION_API_KEY": "gateway-key-at-least-16",
        "OFI_MODEL_VERSION": "v1",
        "OFI_ORDER_NOTIONAL_USD": 25,
        "OFI_MAX_POSITION_NOTIONAL_USD": 30,
        "OFI_MAX_TOTAL_NOTIONAL_USD": 30,
        "OFI_DAILY_LOSS_LIMIT_USD": 3,
        "OFI_MANUAL_APPROVAL_NOTIONAL_USD": 30,
    }
    with pytest.raises(ValueError, match="OFI_MICRO_LIVE_ACK"):
        make_settings(tmp_path, **base)
    ok = make_settings(tmp_path, **base, OFI_MICRO_LIVE_ACK=MICRO_LIVE_ACK)
    assert ok.trading_symbols == ("ETHUSDT",)
    with pytest.raises(ValueError, match="OFI_DAILY_LOSS_LIMIT_USD"):
        make_settings(
            tmp_path, **{**base, "OFI_DAILY_LOSS_LIMIT_USD": 10}, OFI_MICRO_LIVE_ACK=MICRO_LIVE_ACK
        )
    with pytest.raises(ValueError, match="OFI_MODEL_VERSION"):
        make_settings(
            tmp_path,
            **{k: v for k, v in base.items() if k != "OFI_MODEL_VERSION"},
            OFI_MICRO_LIVE_ACK=MICRO_LIVE_ACK,
        )
    with pytest.raises(ValueError, match="OFI_ALLOW_UNGATED"):
        make_settings(tmp_path, **base, OFI_MICRO_LIVE_ACK=MICRO_LIVE_ACK, OFI_ALLOW_UNGATED="true")
    with pytest.raises(ValueError, match="live is refused"):
        make_settings(tmp_path, OFI_EXECUTION_MODE="live")
    assert make_settings(tmp_path).trading_symbols == ("BTCUSDT", "ETHUSDT")


def test_shadow_fills_default_to_the_research_queue_model_at_measured_latency(
    tmp_path, monkeypatch
) -> None:
    import json

    from ofi_scalper_service import app

    latency = tmp_path / "latency.json"
    latency.write_text(json.dumps({"rest_rtt_ms": {"signed": {"p50": 12.0}}}))
    monkeypatch.setattr(app, "default_latency_path", lambda: latency)
    settings = make_settings(tmp_path)
    assert settings.shadow_queue_model == "queue" and settings.shadow_delay_ms is None
    assert app.shadow_fills(settings) == {"queue_model": "queue", "delay_ns": 6_000_000}

    monkeypatch.setattr(app, "default_latency_path", lambda: None)
    assert app.shadow_fills(settings)["delay_ns"] == 0

    pinned = make_settings(
        tmp_path, OFI_SHADOW_QUEUE_MODEL="pessimistic", OFI_SHADOW_DELAY_MS="2.5"
    )
    assert app.shadow_fills(pinned) == {"queue_model": "pessimistic", "delay_ns": 2_500_000}
    with pytest.raises(ValidationError):
        make_settings(tmp_path, OFI_SHADOW_QUEUE_MODEL="optimistic")
