from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from lux_algo.broker_specs import BrokerSpec, lookup_spec
from lux_algo.calibrate import (
    STATUS_CAPPED,
    STATUS_NO_DATA,
    STATUS_NO_SPEC,
    STATUS_OK,
    STATUS_UNSATISFIABLE,
    STATUS_VOLUME_CAPPED,
    RiskTargets,
    build_manifest,
    calibrate_instrument,
    derive_atr_multiple,
    median_atr,
    quantize_volume,
)
from lux_algo.candles import Candle
from lux_algo.config import Settings
from lux_algo.instruments import InstrumentConfig, load_instruments_from_file

TARGETS = RiskTargets(risk_usd=Decimal("25"), reward_usd=Decimal("40"))


def make_settings(**overrides: object) -> Settings:
    base = {
        "data_api_url": "https://data.example.com/candles",
        "quote": "EURUSD",
        "mt5_symbol": "EURUSD",
        "volume": Decimal("0.10"),
        "mt5_signal_api_key": "unit-test-key",
        "price_digits": 5,
        "pip_size_override": 0.0001,
        "stop_loss_pips": 25.0,
        "take_profit_pips": 40.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def deriv_spec(volume_min: str = "0.50", volume_max: str = "100") -> BrokerSpec:
    return BrokerSpec(
        contract_size=Decimal("1"),
        volume_min=Decimal(volume_min),
        volume_step=Decimal("0.01"),
        volume_max=Decimal(volume_max),
    )


def test_volume_rounds_down_onto_the_broker_grid() -> None:
    spec = deriv_spec(volume_min="0.20")
    # 0.20 + n * 0.01 grid: 0.734 rounds down to 0.73, never up.
    assert quantize_volume(Decimal("0.734"), spec) == Decimal("0.73")
    assert quantize_volume(Decimal("0.20"), spec) == Decimal("0.20")


def test_volume_never_falls_below_the_minimum() -> None:
    spec = deriv_spec(volume_min="4.00")
    assert quantize_volume(Decimal("0.01"), spec) == Decimal("4.00")


def test_volume_clamped_to_maximum_stays_on_the_grid() -> None:
    spec = BrokerSpec(
        contract_size=Decimal("1"),
        volume_min=Decimal("0.10"),
        volume_step=Decimal("0.25"),
        volume_max=Decimal("1.00"),
    )
    # Grid is 0.10, 0.35, 0.60, 0.85, (1.10 > max) -> 0.85.
    assert quantize_volume(Decimal("5"), spec) == Decimal("0.85")


def test_stop_sized_from_atr_and_lot_holds_the_risk_budget() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(
        quote="Volatility 100 Index",
        pip_size=0.01,
        price_digits=2,
        volume=Decimal("1.00"),
        stop_loss_pips=169.0,
        take_profit_pips=338.0,
    )
    # ATR 5.0 x multiple 2.0 = a 10.00 price stop = 1000 pips at pip_size 0.01.
    result = calibrate_instrument(
        instrument,
        settings,
        deriv_spec(volume_min="0.50"),
        atr_value=5.0,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.status == STATUS_OK
    assert result.stop_loss_pips == 1000
    assert result.take_profit_pips == 1600
    assert result.volume == Decimal("2.50")  # 25 / 10.00
    assert result.risk_usd == Decimal("25.00")
    assert result.reward_usd == Decimal("40.00")


def test_take_profit_keeps_the_reward_ratio() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(quote="XAUUSD", pip_size=0.10, stop_loss_pips=25.0)
    result = calibrate_instrument(
        instrument,
        settings,
        lookup_spec("XAUUSD"),
        atr_value=1.25,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.status == STATUS_OK
    assert result.stop_loss_pips == 25  # 1.25 x 2 = 2.50 price = 25 pips
    assert result.take_profit_pips == 40
    assert result.volume == Decimal("0.10")  # 25 / (2.50 x 100)
    assert result.risk_usd == Decimal("25.000")
    assert result.reward_usd == Decimal("40.000")


def test_stop_never_goes_under_the_existing_broker_floor() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(
        quote="Volatility 75 Index",
        pip_size=0.01,
        volume=Decimal("0.01"),
        stop_loss_pips=12587.0,
    )
    # A tiny ATR would ask for a stop far inside trade_stops_level; the floor wins.
    result = calibrate_instrument(
        instrument,
        settings,
        deriv_spec(volume_min="0.01", volume_max="30"),
        atr_value=1.0,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.stop_loss_pips == 12587
    assert result.risk_usd is not None and result.risk_usd <= TARGETS.risk_usd


def test_minimum_lot_overshoot_shrinks_the_stop_to_hold_the_budget() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(
        quote="Volatility 50 Index",
        pip_size=0.0001,
        volume=Decimal("4.00"),
        stop_loss_pips=100.0,
    )
    # ATR 5.0 x 2 = a 10.00 stop; at the 4.00 minimum lot that is $40, over budget.
    result = calibrate_instrument(
        instrument,
        settings,
        deriv_spec(volume_min="4.00"),
        atr_value=5.0,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.status == STATUS_CAPPED
    assert result.volume == Decimal("4.00")
    assert result.stop_loss_pips == 62500  # 25 / 4.00 = 6.25 price = 62500 pips
    assert result.risk_usd == Decimal("25.0000")


def test_unsatisfiable_when_the_shrunk_stop_falls_under_the_floor() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(
        quote="Volatility 50 Index",
        pip_size=0.0001,
        volume=Decimal("4.00"),
        stop_loss_pips=100_000.0,  # floor of 10.00 in price terms
    )
    result = calibrate_instrument(
        instrument,
        settings,
        deriv_spec(volume_min="4.00"),
        atr_value=5.0,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.status == STATUS_UNSATISFIABLE
    assert result.stop_loss_pips is None
    assert result.volume is None
    assert "$40.00" in result.note


def test_lot_capped_at_the_broker_maximum() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(quote="Step Index", pip_size=0.1, stop_loss_pips=20.0)
    result = calibrate_instrument(
        instrument,
        settings,
        deriv_spec(volume_min="0.10", volume_max="1.00"),
        atr_value=1.0,
        sl_atr_multiple=Decimal("2"),
        targets=TARGETS,
    )
    assert result.status == STATUS_VOLUME_CAPPED
    assert result.volume == Decimal("1.00")
    assert result.risk_usd is not None and result.risk_usd < TARGETS.risk_usd


def test_missing_spec_and_missing_data_are_reported_not_applied() -> None:
    settings = make_settings()
    instrument = InstrumentConfig(quote="Unknown Index", pip_size=0.01, stop_loss_pips=10.0)
    no_spec = calibrate_instrument(
        instrument, settings, None, 1.0, sl_atr_multiple=Decimal("2"), targets=TARGETS
    )
    no_data = calibrate_instrument(
        instrument, settings, deriv_spec(), None, sl_atr_multiple=Decimal("2"), targets=TARGETS
    )
    assert no_spec.status == STATUS_NO_SPEC
    assert no_data.status == STATUS_NO_DATA
    assert not no_spec.applied
    assert not no_data.applied


def test_derive_atr_multiple_from_the_reference_stop() -> None:
    settings = make_settings()
    gold = InstrumentConfig(quote="XAUUSD", pip_size=0.10, stop_loss_pips=25.0)
    # 25 pips x 0.10 = a $2.50 stop; against a $1.25 ATR that is 2 ATR of room.
    assert derive_atr_multiple(gold, settings, 1.25) == Decimal("2")


def test_derive_atr_multiple_rejects_a_dead_reference() -> None:
    settings = make_settings()
    gold = InstrumentConfig(quote="XAUUSD", pip_size=0.10, stop_loss_pips=25.0)
    with pytest.raises(ValueError, match="no usable ATR"):
        derive_atr_multiple(gold, settings, 0.0)


def test_median_atr_uses_closed_target_timeframe_bars() -> None:
    settings = make_settings(target_tf_minutes=3, supertrend_atr_len=2)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    minutes = [
        Candle(
            start=start + timedelta(minutes=i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(30)
    ]
    value = median_atr(minutes, settings)
    assert value is not None and value == pytest.approx(2.0)


def test_median_atr_returns_none_without_enough_history() -> None:
    settings = make_settings(target_tf_minutes=3, supertrend_atr_len=11)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    minutes = [
        Candle(
            start=start + timedelta(minutes=i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(6)
    ]
    assert median_atr(minutes, settings) is None


def test_manifest_round_trips_and_leaves_unapplied_entries_untouched(tmp_path: Path) -> None:
    settings = make_settings()
    instruments = [
        InstrumentConfig(
            quote="Volatility 100 Index",
            pip_size=0.01,
            price_digits=2,
            volume=Decimal("1.00"),
            deviation_points=50,
            stop_loss_pips=169.0,
            take_profit_pips=338.0,
        ),
        InstrumentConfig(
            quote="Unknown Index",
            pip_size=0.01,
            price_digits=2,
            volume=Decimal("0.10"),
            deviation_points=50,
            stop_loss_pips=10.0,
            take_profit_pips=20.0,
        ),
    ]
    results = {
        inst.quote: calibrate_instrument(
            inst,
            settings,
            lookup_spec(inst.resolved_mt5_symbol()) or deriv_spec()
            if inst.quote.startswith("Volatility")
            else None,
            5.0 if inst.quote.startswith("Volatility") else None,
            sl_atr_multiple=Decimal("2"),
            targets=TARGETS,
        )
        for inst in instruments
    }
    manifest = build_manifest(instruments, settings, results)

    assert manifest[0]["stop_loss_pips"] == 1000
    assert manifest[0]["take_profit_pips"] == 1600
    assert manifest[0]["volume"] == "2.50"
    # Untouched entry keeps its original values, as whole numbers rather than floats.
    assert manifest[1]["stop_loss_pips"] == 10
    assert manifest[1]["volume"] == "0.10"

    path = tmp_path / "symbols.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    reloaded = load_instruments_from_file(path)
    assert [i.quote for i in reloaded] == [i.quote for i in instruments]
    assert reloaded[0].volume == Decimal("2.50")
    assert reloaded[0].stop_loss_pips == 1000
